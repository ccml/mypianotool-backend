"""Moteur Audiveris, appelé par sa ligne de commande.

    Audiveris -batch -export -output <dossier> -- <image>

Audiveris est écrit en Java et ne s'embarque pas dans un processus Python : le
sous-processus n'est pas un choix de confort, c'est le seul chemin. Il écrit un
`.mxl` (ou un `.xml` si l'on désactive la compression) et un `.omr`, son fichier
de projet, dans le dossier de sortie.

Ce qu'il attend, par rapport à oemer : une page nette et bien résolue. Il fait sa
propre binarisation et son propre redressement, mais il a besoin d'un interligne
d'une quinzaine de pixels au moins — d'où la résolution plus haute demandée dans
`image.py` depuis qu'il est le moteur par défaut.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..config import config
from .base import EchecMoteur, Moteur, Progression, ResultatMoteur

logger = logging.getLogger(__name__)

# Sortie en MusicXML non compressé : un fichier de moins à décompresser.
# (Le `.mxl` reste géré en aval, au cas où la constante changerait de nom.)
CONSTANTE_COMPRESSION = "org.audiveris.omr.sheet.BookManager.useCompression=false"

#
# Audiveris ne journalise pas ses vingt étapes en mode batch : il n'y a donc pas
# de progression exacte à lire. Plutôt que d'inventer des étapes qu'on n'a pas
# vues, on n'annonce que les quelques lignes qu'il écrit réellement, et la barre
# avance avec le temps entre deux.
#
MARQUEURS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"\bSheet\b.*\bloaded\b|\bLoading\b", re.I), 12, "Chargement de la page"),
    (re.compile(r"Scale\s*\{|\binterline\b", re.I), 30, "Portées repérées"),
    (re.compile(r"\bstaff|\bsystem[s]?\b.*\bfound\b", re.I), 45, "Portées et systèmes"),
    (re.compile(r"\bBook stored as\b", re.I), 88, "Écriture du résultat"),
    (re.compile(r"\.mxl\b|\.xml\b|\bexport", re.I), 93, "Écriture du MusicXML"),
]

# Ordre de grandeur d'une page sur un cœur, pour faire avancer la barre.
DUREE_ESTIMEE_S = 150

# Ce qu'Audiveris écrit à côté du MusicXML et qui n'en est pas.
EXTENSIONS_RESULTAT = (".musicxml", ".xml", ".mxl")


class MoteurAudiveris(Moteur):
    nom = "audiveris"

    def __init__(self) -> None:
        self._executable = self._trouver_executable()
        # Demander sa version à Audiveris démarre une JVM : on ne le fait qu'une
        # fois, puisque /api/sante est interrogé à chaque ouverture de la fenêtre.
        self._version: str | None = None
        self._version_demandee = False

    @staticmethod
    def _trouver_executable() -> str | None:
        """`MPT_AUDIVERIS_CMD` d'abord, puis le PATH, sous ses deux orthographes."""
        indique = os.environ.get("MPT_AUDIVERIS_CMD", "").strip()
        if indique:
            chemin = Path(indique)
            if chemin.is_file() and os.access(chemin, os.X_OK):
                return str(chemin)
            trouve = shutil.which(indique)
            if trouve:
                return trouve
            # Indiqué mais introuvable : on ne retombe pas en silence sur le PATH,
            # sinon l'erreur affichée parlerait d'autre chose que du vrai problème.
            return None
        return shutil.which("Audiveris") or shutil.which("audiveris")

    def pret(self) -> bool:
        return self._executable is not None

    def version(self) -> str | None:
        if self._executable is None:
            return None
        if self._version_demandee:
            return self._version
        self._version_demandee = True
        self._version = self._interroger_version()
        return self._version

    def _interroger_version(self) -> str | None:
        """`-batch -help` : la 5.9 n'a pas de `-version` (il arrive en 5.10).

        Avant d'afficher son aide, Audiveris journalise son environnement — sa
        version, et surtout l'état de son OCR, qui est ce qu'on veut vraiment
        savoir avant le premier scan.
        """
        assert self._executable is not None
        try:
            sortie = subprocess.run(
                [self._executable, "-batch", "-help"],
                capture_output=True,
                text=True,
                timeout=180,
                env=self._environnement(),
            )
        except Exception:
            return None
        return _extraire_version(sortie.stdout + sortie.stderr)

    def _environnement(self) -> dict[str, str]:
        environnement = dict(os.environ)
        # Pas d'écran : sans cela, la JVM cherche un serveur X et meurt en conteneur.
        options = [
            "-Djava.awt.headless=true",
            f"-Xmx{os.environ.get('MPT_AUDIVERIS_MEMOIRE', '3g')}",
        ]
        existant = environnement.get("JAVA_OPTS", "").strip()
        environnement["JAVA_OPTS"] = " ".join(filter(None, [existant, *options]))
        return environnement

    def _commande(self, image: Path, dossier_sortie: Path) -> list[str]:
        assert self._executable is not None
        return [
            self._executable,
            "-batch",
            "-export",
            "-constant",
            CONSTANTE_COMPRESSION,
            "-output",
            str(dossier_sortie),
            "--",
            str(image),
        ]

    def transcrire(
        self,
        image: Path,
        dossier_sortie: Path,
        progression: Progression,
    ) -> ResultatMoteur:
        if self._executable is None:
            raise EchecMoteur(
                "Audiveris n'est pas installé dans cet environnement. Donnez son chemin "
                "dans MPT_AUDIVERIS_CMD, utilisez l'image Docker qui l'embarque, "
                "ou passez à MPT_MOTEUR=factice."
            )

        dossier_sortie.mkdir(parents=True, exist_ok=True)
        commande = self._commande(image, dossier_sortie)
        logger.info("audiveris : %s", " ".join(commande))
        progression(5, "Démarrage de la reconnaissance")

        debut = time.monotonic()
        journal: list[str] = []
        atteint = 5

        processus = subprocess.Popen(
            commande,
            cwd=str(dossier_sortie),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            env=self._environnement(),
        )

        try:
            assert processus.stdout is not None
            for ligne in processus.stdout:
                ligne = ligne.rstrip()
                if not ligne:
                    continue
                journal.append(ligne)
                if len(journal) > 400:
                    del journal[:100]

                for motif, pourcent, libelle in MARQUEURS:
                    if motif.search(ligne) and pourcent > atteint:
                        atteint = pourcent
                        progression(atteint, libelle)
                        break
                else:
                    ecoule = time.monotonic() - debut
                    estime = min(85, 5 + int(80 * ecoule / DUREE_ESTIMEE_S))
                    if estime > atteint:
                        atteint = estime
                        progression(atteint, "Analyse de la page")

                if time.monotonic() - debut > config.delai_moteur_s:
                    raise EchecMoteur(
                        f"Reconnaissance interrompue après {config.delai_moteur_s} s."
                    )

            code = processus.wait(timeout=120)
        except EchecMoteur:
            processus.kill()
            processus.wait(timeout=30)
            raise
        except subprocess.TimeoutExpired as erreur:
            processus.kill()
            raise EchecMoteur("Audiveris ne s'est pas terminé.") from erreur
        finally:
            if processus.poll() is None:
                processus.kill()

        musicxml = _trouver_musicxml(dossier_sortie)
        if musicxml is None:
            # Audiveris sort souvent en code 0 sans rien exporter quand il ne
            # trouve pas de portée : c'est le cas le plus fréquent, et le message
            # doit parler de la photo, pas du logiciel.
            raise EchecMoteur(_message_echec(code, journal))
        if code != 0:
            logger.warning("audiveris : code %s mais un MusicXML a été écrit", code)

        progression(96, "Relecture du résultat")
        return ResultatMoteur(
            musicxml=musicxml,
            apercu=None,  # Audiveris ne produit pas d'image d'analyse en mode batch.
            journal=journal[-60:],
        )


def _extraire_version(texte: str) -> str | None:
    """Tire « Audiveris 5.9.0 · OCR Tesseract 5.5.1 » du bloc d'environnement."""
    version: str | None = None
    ocr: str | None = None

    for ligne in texte.splitlines():
        if version is None:
            trouve = re.search(r"-\s*Audiveris:\s*(.+?)\s*$", ligne)
            if trouve:
                version = trouve.group(1)
        if ocr is None:
            trouve = re.search(r"-\s*OCR Engine:\s*(.+?)\s*$", ligne)
            if trouve:
                ocr = trouve.group(1)

    if version is None:
        return None
    return f"Audiveris {version}" + (f" · OCR {ocr}" if ocr else "")


def _trouver_musicxml(dossier: Path) -> Path | None:
    """Le `.xml` ou `.mxl` exporté, en ignorant le `.omr` et les journaux."""
    candidats = [
        p
        for p in dossier.rglob("*")
        if p.is_file() and p.suffix.lower() in EXTENSIONS_RESULTAT and p.stat().st_size > 0
    ]
    if not candidats:
        return None
    return max(candidats, key=lambda p: p.stat().st_mtime)


def _message_echec(code: int, journal: list[str]) -> str:
    """Traduit les reproches d'Audiveris en quelque chose d'actionnable."""
    texte = "\n".join(journal)

    if re.search(r"interline|too small|scale", texte, re.I):
        return (
            "Audiveris n'a pas réussi à mesurer les portées : la photo est "
            "probablement trop petite ou trop floue. Reprenez-la de plus près, "
            "bien à plat et bien éclairée."
        )
    if re.search(r"no staff|No system|not a music|empty", texte, re.I):
        return (
            "Aucune portée n'a été trouvée sur l'image. Cadrez la page entière, "
            "sans angle ni ombre portée."
        )
    if re.search(r"OutOfMemory|heap space", texte, re.I):
        return (
            "Audiveris a manqué de mémoire. Augmentez MPT_AUDIVERIS_MEMOIRE "
            "(3g par défaut) ou réduisez la résolution de l'image."
        )
    if re.search(r"TESSDATA|tessdata|OCR", texte, re.I):
        return (
            "Audiveris n'a pas trouvé les données de l'OCR (tessdata). "
            "Vérifiez la variable TESSDATA_PREFIX du serveur."
        )

    fin = " | ".join(journal[-4:]) if journal else "aucune sortie"
    return f"Audiveris n'a produit aucun MusicXML (code {code}) : {fin}"
