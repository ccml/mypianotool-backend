"""Moteur oemer, appelé par son interface en ligne de commande.

On passe par le sous-processus plutôt que par `oemer.ete` : c'est l'interface que le
projet documente et tient stable, elle isole le serveur des fuites mémoire de
TensorFlow/ONNX sur les grandes images, et elle permet de tuer un traitement qui
s'éternise sans emporter le serveur avec lui.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..config import config
from .base import EchecMoteur, Moteur, Progression, ResultatMoteur

logger = logging.getLogger(__name__)

# oemer décrit ses étapes sur la sortie standard. On n'a aucune garantie sur le
# libellé exact d'une version à l'autre : ce qui n'est pas reconnu est simplement
# ignoré, et la progression retombe sur l'estimation par le temps écoulé.
ETAPES: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"load.*model|初始|loading", re.I), 8, "Chargement des modèles"),
    (re.compile(r"extract.*staff|staffline", re.I), 25, "Détection des portées"),
    (re.compile(r"predict", re.I), 35, "Analyse de l'image"),
    (re.compile(r"extract.*layer|symbol", re.I), 50, "Extraction des symboles"),
    (re.compile(r"not[eh]head|note head", re.I), 62, "Têtes de notes"),
    (re.compile(r"\bnote\b|rhythm|duration", re.I), 72, "Durées et rythme"),
    (re.compile(r"group|voice|chord", re.I), 82, "Voix et accords"),
    (re.compile(r"build|musicxml|output|writ", re.I), 92, "Écriture du MusicXML"),
]

DUREE_ESTIMEE_S = 240  # sert uniquement à faire avancer la barre entre deux étapes


class MoteurOemer(Moteur):
    nom = "oemer"

    def __init__(self) -> None:
        self._executable = shutil.which("oemer")

    def _module_present(self) -> bool:
        try:
            import oemer  # noqa: F401
        except Exception:
            return False
        return True

    def _dossier_modeles(self) -> Path | None:
        try:
            import oemer

            racine = Path(oemer.__file__).parent / "checkpoints"
            return racine if racine.exists() else None
        except Exception:
            return None

    def modeles_presents(self) -> bool:
        """Les poids, pas les dossiers.

        Le paquet installe déjà `unet_big/` et `seg_net/` avec leur `arch.json` :
        vérifier l'existence du dossier ne dit rien. Ce qui manque après un simple
        `pip install`, ce sont les poids, téléchargés au premier lancement.
        """
        dossier = self._dossier_modeles()
        if dossier is None:
            return False
        for nom in ("unet_big", "seg_net"):
            poids = dossier / nom
            if not ((poids / "model.onnx").exists() or (poids / "weights.h5").exists()):
                return False
        return True

    def pret(self) -> bool:
        return (self._executable is not None or self._module_present()) and self.modeles_presents()

    def _commande(self, image: Path, dossier_sortie: Path) -> list[str]:
        if self._executable:
            base = [self._executable]
        else:
            base = [sys.executable, "-m", "oemer"]
        return [*base, str(image), "-o", str(dossier_sortie)]

    def transcrire(
        self,
        image: Path,
        dossier_sortie: Path,
        progression: Progression,
    ) -> ResultatMoteur:
        if self._executable is None and not self._module_present():
            raise EchecMoteur(
                "oemer n'est pas installé dans cet environnement "
                "(pip install oemer, ou utilisez MPT_MOTEUR=factice)."
            )
        if not self.modeles_presents():
            raise EchecMoteur(
                "Les modèles d'oemer sont absents. Lancez scripts/telecharger-modeles.py "
                "ou reconstruisez l'image Docker, qui les embarque."
            )

        dossier_sortie.mkdir(parents=True, exist_ok=True)
        commande = self._commande(image, dossier_sortie)
        logger.info("oemer : %s", " ".join(commande))
        progression(5, "Démarrage de la reconnaissance")

        environnement = dict(os.environ)
        # oemer est très bavard en TensorFlow ; on garde le journal lisible.
        environnement.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        # Sans cela, chaque travail prend tous les cœurs et la file se bloque.
        environnement.setdefault("OMP_NUM_THREADS", str(max(1, (os.cpu_count() or 2) // 2)))

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
            env=environnement,
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

                for motif, pourcent, libelle in ETAPES:
                    if motif.search(ligne) and pourcent > atteint:
                        atteint = pourcent
                        progression(atteint, libelle)
                        break
                else:
                    # Rien de reconnu : on avance doucement avec le temps, sans
                    # jamais dépasser l'étape franchie suivante.
                    ecoule = time.monotonic() - debut
                    estime = min(90, 5 + int(85 * ecoule / DUREE_ESTIMEE_S))
                    if estime > atteint:
                        atteint = estime
                        progression(atteint, "Analyse en cours")

                if time.monotonic() - debut > config.delai_moteur_s:
                    raise EchecMoteur(
                        f"Reconnaissance interrompue après {config.delai_moteur_s} s."
                    )

            code = processus.wait(timeout=60)
        except EchecMoteur:
            processus.kill()
            processus.wait(timeout=30)
            raise
        except subprocess.TimeoutExpired as erreur:
            processus.kill()
            raise EchecMoteur("Le moteur ne s'est pas terminé.") from erreur
        finally:
            if processus.poll() is None:
                processus.kill()

        if code != 0:
            raise EchecMoteur(_message_echec(code, journal))

        musicxml = _trouver_musicxml(dossier_sortie)
        if musicxml is None:
            raise EchecMoteur(
                "Le moteur s'est terminé sans écrire de MusicXML. "
                + _message_echec(code, journal)
            )

        progression(96, "Relecture du résultat")
        return ResultatMoteur(
            musicxml=musicxml,
            apercu=_trouver_apercu(dossier_sortie),
            journal=journal[-60:],
        )


def _trouver_musicxml(dossier: Path) -> Path | None:
    """oemer nomme son fichier d'après l'image ; on ne présume pas du nom."""
    candidats = sorted(
        [p for p in dossier.rglob("*") if p.suffix.lower() in (".musicxml", ".xml", ".mxl")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for candidat in candidats:
        if candidat.suffix.lower() == ".mxl" or candidat.stat().st_size > 0:
            return candidat
    return None


def _trouver_apercu(dossier: Path) -> Path | None:
    """L'image d'analyse qu'oemer dépose à côté, utile à montrer à l'utilisateur."""
    images = [
        p
        for p in dossier.rglob("*")
        if p.suffix.lower() in (".png", ".jpg", ".jpeg") and p.name != "preparee.png"
    ]
    if not images:
        return None
    return max(images, key=lambda p: p.stat().st_size)


def _message_echec(code: int, journal: list[str]) -> str:
    fin = " | ".join(journal[-4:]) if journal else "aucune sortie"
    return f"oemer a échoué (code {code}) : {fin}"
