"""Configuration, entièrement pilotée par variables d'environnement (préfixe MPT_)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _entier(nom: str, defaut: int) -> int:
    valeur = os.environ.get(nom)
    if valeur is None or valeur.strip() == "":
        return defaut
    try:
        return int(valeur)
    except ValueError:
        return defaut


def _texte(nom: str, defaut: str) -> str:
    valeur = os.environ.get(nom)
    return defaut if valeur is None or valeur.strip() == "" else valeur.strip()


def _booleen(nom: str, defaut: bool) -> bool:
    valeur = os.environ.get(nom)
    if valeur is None or valeur.strip() == "":
        return defaut
    return valeur.strip().lower() not in ("0", "non", "false", "off")


def _liste(nom: str, defaut: list[str]) -> list[str]:
    valeur = os.environ.get(nom)
    if valeur is None or valeur.strip() == "":
        return defaut
    return [m.strip() for m in valeur.split(",") if m.strip()]


@dataclass(frozen=True)
class Config:
    # Moteur de reconnaissance : "audiveris", "oemer", ou "factice" (développement
    # et tests). Audiveris par défaut — voir le README pour ce qui a motivé le
    # changement.
    moteur: str = field(default_factory=lambda: _texte("MPT_MOTEUR", "audiveris"))

    hote: str = field(default_factory=lambda: _texte("MPT_HOTE", "0.0.0.0"))
    port: int = field(default_factory=lambda: _entier("MPT_PORT", 8077))

    # Origines autorisées (CORS). Le site tourne en https sur 4200 en développement.
    origines: list[str] = field(
        default_factory=lambda: _liste(
            "MPT_ORIGINES",
            ["https://localhost:4200", "http://localhost:4200"],
        )
    )

    dossier_travaux: Path = field(
        default_factory=lambda: Path(_texte("MPT_DOSSIER_TRAVAUX", "/tmp/mypianotool-omr"))
    )

    taille_max_mo: int = field(default_factory=lambda: _entier("MPT_TAILLE_MAX_MO", 25))

    # Côté image. Audiveris mesure l'interligne pour tout le reste de son analyse
    # et lui faut une quinzaine de pixels : une page A4 scannée à 300 dpi fait
    # 3508 px de haut, et c'est cette résolution-là qu'il faut lui garder. En deçà
    # de la borne basse, il ne trouve plus ses portées.
    largeur_min: int = field(default_factory=lambda: _entier("MPT_LARGEUR_MIN", 1800))
    largeur_max: int = field(default_factory=lambda: _entier("MPT_LARGEUR_MAX", 3508))

    travaux_simultanes: int = field(default_factory=lambda: _entier("MPT_TRAVAUX_SIMULTANES", 1))
    file_max: int = field(default_factory=lambda: _entier("MPT_FILE_MAX", 8))

    # Au delà, le travail est tué : une page qui n'a pas abouti en 15 min n'aboutira pas.
    delai_moteur_s: int = field(default_factory=lambda: _entier("MPT_DELAI_MOTEUR_S", 900))

    # Durée de rétention d'un résultat avant effacement du disque.
    duree_vie_min: int = field(default_factory=lambda: _entier("MPT_DUREE_VIE_MIN", 60))

    limite_ip_par_heure: int = field(default_factory=lambda: _entier("MPT_LIMITE_IP_PAR_HEURE", 20))

    # --- Relais de partage entre appareils -------------------------------
    # Rien de tout cela ne touche le disque : voir `app/relais.py`.

    relais_actif: bool = field(default_factory=lambda: _booleen("MPT_RELAIS", True))

    # Durée de vie d'une partition déposée. Au-delà, le transfert est annulé.
    relais_ttl_s: int = field(default_factory=lambda: _entier("MPT_RELAIS_TTL_S", 180))

    # Même plafond que `MAX_OCTETS` du site (`partage/transfert.ts`) : une
    # partition du modèle, comprimée, fait quelques kilo-octets.
    relais_taille_max_mo: int = field(
        default_factory=lambda: _entier("MPT_RELAIS_TAILLE_MAX_MO", 8)
    )

    # Bornes de la mémoire : un service sans authentification doit savoir dire non.
    relais_max_attentes: int = field(default_factory=lambda: _entier("MPT_RELAIS_MAX_ATTENTES", 100))
    relais_octets_max_total: int = field(
        default_factory=lambda: _entier("MPT_RELAIS_OCTETS_MAX_TOTAL", 64 * 1024 * 1024)
    )

    # Codes erronés tolérés par heure et par adresse : c'est ce qui rend
    # inexploitable l'énumération des 10 000 codes possibles.
    relais_essais_par_heure: int = field(
        default_factory=lambda: _entier("MPT_RELAIS_ESSAIS_PAR_HEURE", 30)
    )
    relais_depots_par_heure: int = field(
        default_factory=lambda: _entier("MPT_RELAIS_DEPOTS_PAR_HEURE", 60)
    )

    @property
    def taille_max_octets(self) -> int:
        return self.taille_max_mo * 1024 * 1024

    @property
    def relais_taille_max_octets(self) -> int:
        return self.relais_taille_max_mo * 1024 * 1024


config = Config()
