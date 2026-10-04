"""Moteur factice : pas de reconnaissance, un MusicXML plausible.

Il sert à deux choses : développer la page Partition sans attendre quatre minutes
par essai, et tester toute la chaîne HTTP sans installer oemer. Il imite le vrai
moteur sur ce qui compte pour l'appelant — la durée, les étapes, et un fichier qui
contient délibérément une mesure fausse, pour que le compte rendu ait de quoi dire.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .base import Moteur, Progression, ResultatMoteur

DUREE_S = float(os.environ.get("MPT_FACTICE_DUREE_S", "1.5"))

ETAPES = [
    (15, "Détection des portées"),
    (40, "Extraction des symboles"),
    (65, "Durées et rythme"),
    (85, "Voix et accords"),
    (95, "Écriture du MusicXML"),
]

# Quatre mesures en do majeur, main droite et main gauche. La mesure 3 de la main
# droite ne compte que trois temps : c'est volontaire, elle doit ressortir comme
# suspecte dans le compte rendu.
MUSICXML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN"
  "http://www.musicxml.org/dtds/partwise.dtd">
<score-partwise version="4.0">
  <work><work-title>Scan factice</work-title></work>
  <part-list>
    <score-part id="P1"><part-name>Piano</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>2</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <staves>2</staves>
        <clef number="1"><sign>G</sign><line>2</line></clef>
        <clef number="2"><sign>F</sign><line>4</line></clef>
      </attributes>
      {MD1}
      <backup><duration>8</duration></backup>
      {MG1}
    </measure>
    <measure number="2">{MD2}<backup><duration>8</duration></backup>{MG2}</measure>
    <measure number="3">{MD3}<backup><duration>6</duration></backup>{MG3}</measure>
    <measure number="4">{MD4}<backup><duration>8</duration></backup>{MG4}
      <barline location="right"><bar-style>light-heavy</bar-style></barline>
    </measure>
  </part>
</score-partwise>
"""


def _note(pas: str, octave: int, duree: int, type_: str, portee: int, voix: int) -> str:
    return (
        "<note>"
        f"<pitch><step>{pas}</step><octave>{octave}</octave></pitch>"
        f"<duration>{duree}</duration><voice>{voix}</voice><type>{type_}</type>"
        f"<staff>{portee}</staff>"
        "</note>"
    )


def _ronde(pas: str, octave: int, portee: int, voix: int, duree: int = 8) -> str:
    return _note(pas, octave, duree, "whole", portee, voix)


def _construire() -> str:
    md = {
        "MD1": "".join(_note(p, 4, 2, "quarter", 1, 1) for p in ("C", "D", "E", "F")),
        "MD2": "".join(_note(p, 4, 2, "quarter", 1, 1) for p in ("G", "A", "G", "F")),
        # Trois noires seulement : la mesure ne tombe pas juste, et c'est le but.
        "MD3": "".join(_note(p, 4, 2, "quarter", 1, 1) for p in ("E", "D", "C")),
        "MD4": _ronde("C", 4, 1, 1),
    }
    mg = {
        "MG1": _ronde("C", 3, 2, 5),
        "MG2": _ronde("F", 2, 2, 5),
        "MG3": _ronde("G", 2, 2, 5, duree=6),
        "MG4": _ronde("C", 3, 2, 5),
    }
    sortie = MUSICXML
    for cle, valeur in {**md, **mg}.items():
        sortie = sortie.replace("{" + cle + "}", valeur)
    return sortie


class MoteurFactice(Moteur):
    nom = "factice"

    def pret(self) -> bool:
        return True

    def transcrire(
        self,
        image: Path,
        dossier_sortie: Path,
        progression: Progression,
    ) -> ResultatMoteur:
        dossier_sortie.mkdir(parents=True, exist_ok=True)
        pas = DUREE_S / len(ETAPES)
        for pourcent, libelle in ETAPES:
            time.sleep(pas)
            progression(pourcent, libelle)

        destination = dossier_sortie / "factice.musicxml"
        destination.write_text(_construire(), encoding="utf-8")
        return ResultatMoteur(
            musicxml=destination,
            apercu=image if image.exists() else None,
            journal=["moteur factice : aucune reconnaissance n'a eu lieu"],
        )
