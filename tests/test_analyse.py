"""Tests du compte rendu tiré du MusicXML."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analyse import analyser  # noqa: E402
from app.moteurs.factice import _construire  # noqa: E402

MESURE_JUSTE = """<?xml version="1.0"?>
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions><key><fifths>2</fifths></key>
        <time><beats>3</beats><beat-type>4</beat-type></time></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
      <note><pitch><step>E</step><octave>4</octave></pitch><duration>1</duration><chord/></note>
      <note><rest/><duration>2</duration></note>
    </measure>
  </part>
</score-partwise>
"""


def test_mesure_juste_nest_pas_signalee():
    rapport = analyser(MESURE_JUSTE)
    assert rapport.mesures == 1
    assert rapport.notes == 2
    assert rapport.accords == 1
    assert rapport.silences == 1
    assert rapport.chiffrage == "3/4"
    assert rapport.armure == 2
    assert rapport.mesures_suspectes == []


def test_mesure_fausse_est_signalee():
    rapport = analyser(_construire())
    assert rapport.mesures == 4
    assert rapport.mesures_suspectes == [3]
    assert rapport.mesures_suspectes_total == 1


def test_levee_en_premiere_mesure_toleree():
    xml = MESURE_JUSTE.replace(
        "<note><rest/><duration>2</duration></note>", ""
    )  # une seule noire sur une mesure à 3 temps
    rapport = analyser(xml)
    assert rapport.mesures_suspectes == []


def test_mesure_courte_ailleurs_quen_premiere_est_signalee():
    xml = MESURE_JUSTE.replace(
        "</part>",
        """<measure number="2">
             <note><pitch><step>D</step><octave>4</octave></pitch><duration>1</duration></note>
           </measure></part>""",
    )
    rapport = analyser(xml)
    assert rapport.mesures_suspectes == [2]


def test_musicxml_illisible():
    rapport = analyser("<score-partwise><part>")
    assert rapport.avertissements
    assert "illisible" in rapport.avertissements[0]


def test_aucune_note_avertit():
    xml = """<score-partwise><part-list/><part id="P1"><measure number="1"/></part></score-partwise>"""
    rapport = analyser(xml)
    assert any("Aucune note" in a for a in rapport.avertissements)


def test_timewise_refuse():
    rapport = analyser("<score-timewise/>")
    assert any("timewise" in a for a in rapport.avertissements)


if __name__ == "__main__":
    for nom, fonction in sorted(list(globals().items())):
        if nom.startswith("test_") and callable(fonction):
            fonction()
            print("ok", nom)
    print("tous les tests d'analyse passent")
