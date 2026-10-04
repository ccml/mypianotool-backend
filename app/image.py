"""Préparation de l'image avant reconnaissance.

Ce que l'on reçoit : une photo prise au téléphone et choisie dans la photothèque,
ou un scan, ou la première page d'un PDF. Ce que le moteur veut : une image
droite, contrastée, et surtout assez grande — Audiveris mesure l'interligne des
portées avant tout le reste, et il lui faut une quinzaine de pixels.

Le redressement et la binarisation ne sont pas faits ici : Audiveris a les siens,
meilleurs, parce qu'ils s'appuient sur les portées qu'il vient de trouver.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .config import config

logger = logging.getLogger(__name__)

# HEIC : ce que donne un iPhone quand Safari ne convertit pas à l'envoi.
try:  # pragma: no cover - dépend de l'environnement
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_DISPONIBLE = True
except Exception:  # pragma: no cover
    HEIC_DISPONIBLE = False

try:  # pragma: no cover - dépend de l'environnement
    import pypdfium2

    PDF_DISPONIBLE = True
except Exception:  # pragma: no cover
    PDF_DISPONIBLE = False


class ImageInvalide(Exception):
    """Le fichier reçu n'est pas une image exploitable."""


@dataclass
class ImagePreparee:
    chemin: Path
    largeur: int
    hauteur: int
    source_pdf: bool
    remarques: list[str]


def _est_pdf(donnees: bytes) -> bool:
    return donnees[:5] == b"%PDF-"


def _pdf_vers_image(source: Path, destination: Path) -> Path:
    if not PDF_DISPONIBLE:
        raise ImageInvalide(
            "Ce fichier est un PDF et le serveur n'a pas pypdfium2 ; "
            "envoyez une image (JPEG, PNG) ou installez la dépendance."
        )
    document = pypdfium2.PdfDocument(str(source))
    try:
        if len(document) == 0:
            raise ImageInvalide("PDF vide.")
        page = document[0]
        # 300 dpi : l'échelle est en multiples de 72 dpi.
        rendu = page.render(scale=300 / 72)
        rendu.to_pil().save(destination)
    finally:
        document.close()
    return destination


def preparer(donnees: bytes, nom_origine: str, dossier: Path) -> ImagePreparee:
    """Écrit dans `dossier` une image PNG prête pour le moteur et la décrit."""

    dossier.mkdir(parents=True, exist_ok=True)
    remarques: list[str] = []
    source_pdf = _est_pdf(donnees)

    brut = dossier / ("source.pdf" if source_pdf else "source.bin")
    brut.write_bytes(donnees)

    if source_pdf:
        chemin_image = _pdf_vers_image(brut, dossier / "page.png")
        remarques.append("PDF : seule la première page est analysée.")
        ouverture = chemin_image
    else:
        ouverture = brut

    try:
        with Image.open(ouverture) as image:
            image.load()
            # Une photo porte son orientation dans l'EXIF ; sans cela, une partition
            # prise en paysage arrive couchée et aucune portée n'est trouvée.
            image = ImageOps.exif_transpose(image)
            image = image.convert("L")

            largeur, hauteur = image.size
            if largeur < 400 or hauteur < 400:
                raise ImageInvalide(
                    f"Image trop petite ({largeur}×{hauteur}) : il faut au moins 400 px de côté."
                )

            cote_max = max(largeur, hauteur)
            if cote_max > config.largeur_max:
                facteur = config.largeur_max / cote_max
                image = image.resize(
                    (round(largeur * facteur), round(hauteur * facteur)),
                    Image.LANCZOS,
                )
                remarques.append(f"Image réduite à {image.size[0]}×{image.size[1]}.")
            elif cote_max < config.largeur_min:
                facteur = config.largeur_min / cote_max
                image = image.resize(
                    (round(largeur * facteur), round(hauteur * facteur)),
                    Image.LANCZOS,
                )
                remarques.append(
                    "Image agrandie : à cette résolution, la reconnaissance est peu fiable."
                )

            # Égalise un éclairage inégal — l'ombre de la main sur la page, typiquement.
            image = ImageOps.autocontrast(image, cutoff=1)

            destination = dossier / "preparee.png"
            image.save(destination, format="PNG", optimize=False)
            taille = image.size
    except ImageInvalide:
        raise
    except Exception as erreur:  # format inconnu, fichier tronqué, HEIC sans pillow-heif
        indice = ""
        if not HEIC_DISPONIBLE and nom_origine.lower().endswith((".heic", ".heif")):
            indice = " Le serveur ne sait pas lire le HEIC (pillow-heif absent)."
        raise ImageInvalide(f"Fichier illisible comme image.{indice}") from erreur

    return ImagePreparee(
        chemin=destination,
        largeur=taille[0],
        hauteur=taille[1],
        source_pdf=source_pdf,
        remarques=remarques,
    )
