"""Choix du moteur de reconnaissance.

`audiveris` par défaut depuis le 4 octobre 2026 : oemer ne rendait rien
d'exploitable sur les photos d'essai. oemer reste disponible, mais son
installation est devenue facultative (voir `requirements-oemer.txt`).
"""

from __future__ import annotations

import logging

from ..config import config
from .audiveris_moteur import MoteurAudiveris
from .base import EchecMoteur, Moteur, ResultatMoteur
from .factice import MoteurFactice

logger = logging.getLogger(__name__)

MOTEURS = ("audiveris", "oemer", "factice")

_moteur: Moteur | None = None


def moteur() -> Moteur:
    global _moteur
    if _moteur is None:
        _moteur = _construire(config.moteur)
        logger.info("Moteur retenu : %s (prêt : %s)", _moteur.nom, _moteur.pret())
    return _moteur


def _construire(nom: str) -> Moteur:
    if nom == "factice":
        return MoteurFactice()
    if nom == "oemer":
        # Importé seulement ici : son module n'a aucune dépendance exotique, mais
        # le laisser en bas de page signale qu'il n'est plus le chemin principal.
        from .oemer_moteur import MoteurOemer

        return MoteurOemer()
    if nom != "audiveris":
        logger.warning("MPT_MOTEUR=%s inconnu ; audiveris est utilisé.", nom)
    return MoteurAudiveris()


__all__ = ["EchecMoteur", "MOTEURS", "Moteur", "ResultatMoteur", "moteur"]
