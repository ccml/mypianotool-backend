"""Tests du moteur oemer sans oemer installé, et de la lecture des .mxl.

Ce qui est vérifié ici, c'est le comportement du serveur quand le moteur manque
ou rend autre chose qu'attendu — le cas le plus probable en production.
"""

from __future__ import annotations

import io
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.moteurs.base import EchecMoteur  # noqa: E402
from app.moteurs.oemer_moteur import (  # noqa: E402
    MoteurOemer,
    _trouver_apercu,
    _trouver_musicxml,
)
from app.travaux import _extraire_mxl  # noqa: E402

MINIMAL = b'<?xml version="1.0"?><score-partwise version="4.0"><part-list/></score-partwise>'


def test_moteur_absent_donne_un_message_clair():
    moteur = MoteurOemer()
    if moteur.pret():  # oemer est installé ici : rien à vérifier
        return
    with tempfile.TemporaryDirectory() as dossier:
        try:
            moteur.transcrire(Path(dossier) / "x.png", Path(dossier) / "out", lambda p, e: None)
        except EchecMoteur as erreur:
            message = str(erreur)
            assert "oemer" in message
            assert "factice" in message or "modèles" in message
        else:
            raise AssertionError("un moteur absent devrait échouer explicitement")


def test_trouver_musicxml_prend_le_plus_recent():
    with tempfile.TemporaryDirectory() as dossier:
        racine = Path(dossier)
        (racine / "vieux.xml").write_bytes(MINIMAL)
        sous = racine / "sortie"
        sous.mkdir()
        recent = sous / "partition.musicxml"
        recent.write_bytes(MINIMAL)
        import os
        import time

        os.utime(racine / "vieux.xml", (time.time() - 100, time.time() - 100))
        assert _trouver_musicxml(racine) == recent


def test_trouver_musicxml_absent():
    with tempfile.TemporaryDirectory() as dossier:
        assert _trouver_musicxml(Path(dossier)) is None


def test_apercu_ignore_limage_preparee():
    with tempfile.TemporaryDirectory() as dossier:
        racine = Path(dossier)
        (racine / "preparee.png").write_bytes(b"0" * 5000)
        analyse = racine / "partition_analyse.png"
        analyse.write_bytes(b"0" * 100)
        assert _trouver_apercu(racine) == analyse


def test_extraction_mxl_par_le_container():
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="score.xml"/></rootfiles></container>',
        )
        archive.writestr("score.xml", MINIMAL.decode())
        archive.writestr("leurre.xml", "<faux/>")
    assert b"score-partwise" in _extraire_mxl(tampon.getvalue())


def test_extraction_mxl_sans_container():
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w") as archive:
        archive.writestr("musique.musicxml", MINIMAL.decode())
    assert b"score-partwise" in _extraire_mxl(tampon.getvalue())


if __name__ == "__main__":
    for nom, fonction in sorted(list(globals().items())):
        if nom.startswith("test_") and callable(fonction):
            fonction()
            print("ok", nom)
    print("tous les tests de moteur passent")
