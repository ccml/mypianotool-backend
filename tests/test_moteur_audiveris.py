"""Tests du moteur Audiveris, sans Audiveris installé.

On ne peut pas vérifier ici la qualité de la reconnaissance — il faut la vraie
JVM et une vraie photo. Ce qui est vérifié, c'est tout ce qui l'entoure : la
commande construite, la lecture du résultat, et surtout la traduction des échecs
en un message qui parle de la photo plutôt que du logiciel.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.moteurs.audiveris_moteur import (  # noqa: E402
    CONSTANTE_COMPRESSION,
    MoteurAudiveris,
    _extraire_version,
    _message_echec,
    _trouver_musicxml,
)

# Ce qu'Audiveris 5.9 journalise avant d'afficher son aide (Main.showEnvironment).
ENVIRONNEMENT = """\
INFO  []                       CLI 282  | CLI args: [-batch, -help]
INFO  []                      Main 318  | Environment:
- Audiveris:    5.9.0:7cd2559
- OS:           Linux 6.10.0
- Architecture: aarch64
- Java VM:      OpenJDK 64-Bit Server VM (build 25+37, mixed mode)
- OCR Engine:   Tesseract OCR, version 5.5.1
"""
from app.moteurs.base import EchecMoteur  # noqa: E402

MINIMAL = b'<?xml version="1.0"?><score-partwise version="4.0"><part-list/></score-partwise>'


def _faux_executable(dossier: Path) -> Path:
    """Un script qui tient lieu d'Audiveris, pour contrôler la commande reçue."""
    chemin = dossier / "Audiveris"
    chemin.write_text("#!/bin/sh\necho \"$@\" > \"$(dirname \"$0\")/arguments.txt\"\n")
    chemin.chmod(0o755)
    return chemin


def test_commande_complete():
    with tempfile.TemporaryDirectory() as dossier:
        racine = Path(dossier)
        os.environ["MPT_AUDIVERIS_CMD"] = str(_faux_executable(racine))
        try:
            moteur = MoteurAudiveris()
            assert moteur.pret()
            commande = moteur._commande(racine / "page.png", racine / "sortie")
        finally:
            del os.environ["MPT_AUDIVERIS_CMD"]

    assert "-batch" in commande
    assert "-export" in commande
    assert CONSTANTE_COMPRESSION in commande
    # Le `--` doit précéder le fichier, sinon un nom commençant par « - » serait
    # pris pour une option.
    assert commande.index("--") == len(commande) - 2
    assert commande[-1].endswith("page.png")
    assert commande[commande.index("-output") + 1].endswith("sortie")


def test_chemin_indique_mais_introuvable():
    os.environ["MPT_AUDIVERIS_CMD"] = "/aucun/chemin/vers/Audiveris"
    try:
        moteur = MoteurAudiveris()
        assert not moteur.pret()
        assert moteur.version() is None
    finally:
        del os.environ["MPT_AUDIVERIS_CMD"]


def test_absence_donne_un_message_actionnable():
    os.environ["MPT_AUDIVERIS_CMD"] = "/aucun/chemin/vers/Audiveris"
    try:
        moteur = MoteurAudiveris()
        with tempfile.TemporaryDirectory() as dossier:
            try:
                moteur.transcrire(Path(dossier) / "p.png", Path(dossier) / "out", lambda p, e: None)
            except EchecMoteur as erreur:
                message = str(erreur)
                assert "MPT_AUDIVERIS_CMD" in message
                assert "factice" in message
            else:
                raise AssertionError("un moteur absent devrait échouer explicitement")
    finally:
        del os.environ["MPT_AUDIVERIS_CMD"]


def test_java_opts_sont_posees():
    os.environ["MPT_AUDIVERIS_CMD"] = "/aucun/chemin"
    os.environ["MPT_AUDIVERIS_MEMOIRE"] = "5g"
    try:
        environnement = MoteurAudiveris()._environnement()
    finally:
        del os.environ["MPT_AUDIVERIS_CMD"]
        del os.environ["MPT_AUDIVERIS_MEMOIRE"]
    assert "-Djava.awt.headless=true" in environnement["JAVA_OPTS"]
    assert "-Xmx5g" in environnement["JAVA_OPTS"]


def test_le_omr_nest_pas_pris_pour_le_resultat():
    with tempfile.TemporaryDirectory() as dossier:
        racine = Path(dossier)
        (racine / "page.omr").write_bytes(b"projet audiveris")
        (racine / "page.log").write_text("journal")
        attendu = racine / "page.xml"
        attendu.write_bytes(MINIMAL)
        assert _trouver_musicxml(racine) == attendu


def test_fichier_vide_ignore():
    with tempfile.TemporaryDirectory() as dossier:
        racine = Path(dossier)
        (racine / "vide.xml").write_bytes(b"")
        assert _trouver_musicxml(racine) is None


def test_mxl_accepte_si_cest_tout_ce_quil_y_a():
    with tempfile.TemporaryDirectory() as dossier:
        racine = Path(dossier)
        archive = racine / "page.mxl"
        archive.write_bytes(b"PK\x03\x04 faux zip")
        assert _trouver_musicxml(racine) == archive


def test_plus_recent_gagne():
    with tempfile.TemporaryDirectory() as dossier:
        racine = Path(dossier)
        vieux = racine / "ancien.xml"
        vieux.write_bytes(MINIMAL)
        time.sleep(0.01)
        recent = racine / "page.xml"
        recent.write_bytes(MINIMAL)
        os.utime(vieux, (time.time() - 500, time.time() - 500))
        assert _trouver_musicxml(racine) == recent


def test_transcription_complete_avec_un_faux_audiveris():
    """Le vrai chemin : sous-processus, lecture du journal, progression, résultat.

    Seule la reconnaissance est remplacée ; tout le reste est le code de production.
    """
    with tempfile.TemporaryDirectory() as dossier:
        racine = Path(dossier)
        faux = racine / "Audiveris"
        faux.write_text(
            "#!/bin/sh\n"
            "echo 'INFO  Loading sheet image'\n"
            "echo 'INFO  Scale{interline=20, line=3}'\n"
            "echo 'INFO  2 systems found'\n"
            "sortie=''\n"
            "while [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = '-output' ]; then shift; sortie=\"$1\"; fi\n"
            "  shift\n"
            "done\n"
            "mkdir -p \"$sortie\"\n"
            "printf '%s' '" + MINIMAL.decode() + "' > \"$sortie/page.xml\"\n"
            "echo \"INFO  Book stored as $sortie/page.omr\"\n"
            ": > \"$sortie/page.omr\"\n"
        )
        faux.chmod(0o755)

        os.environ["MPT_AUDIVERIS_CMD"] = str(faux)
        try:
            moteur = MoteurAudiveris()
            etapes: list[tuple[int, str]] = []
            resultat = moteur.transcrire(
                racine / "page.png",
                racine / "sortie",
                lambda pourcent, etape: etapes.append((pourcent, etape)),
            )
        finally:
            del os.environ["MPT_AUDIVERIS_CMD"]

        assert resultat.musicxml.name == "page.xml"
        assert b"score-partwise" in resultat.musicxml.read_bytes()
        assert resultat.apercu is None
        assert resultat.journal

        pourcentages = [p for p, _ in etapes]
        assert pourcentages == sorted(pourcentages), "la progression doit être croissante"
        assert max(pourcentages) >= 88
        libelles = " ".join(e for _, e in etapes)
        assert "Portées repérées" in libelles


def test_echec_quand_rien_nest_exporte():
    """Audiveris sort en 0 sans rien écrire : c'est le cas le plus fréquent."""
    with tempfile.TemporaryDirectory() as dossier:
        racine = Path(dossier)
        faux = racine / "Audiveris"
        faux.write_text("#!/bin/sh\necho 'WARN  No staff found in sheet'\nexit 0\n")
        faux.chmod(0o755)

        os.environ["MPT_AUDIVERIS_CMD"] = str(faux)
        try:
            try:
                MoteurAudiveris().transcrire(
                    racine / "page.png", racine / "sortie", lambda p, e: None
                )
            except EchecMoteur as erreur:
                assert "portée" in str(erreur).lower()
            else:
                raise AssertionError("sans export, le moteur doit échouer")
        finally:
            del os.environ["MPT_AUDIVERIS_CMD"]


def test_version_lue_dans_lenvironnement():
    version = _extraire_version(ENVIRONNEMENT)
    assert version == "Audiveris 5.9.0:7cd2559 · OCR Tesseract OCR, version 5.5.1"


def test_version_sans_ocr():
    texte = ENVIRONNEMENT.replace("Tesseract OCR, version 5.5.1", "no OCR")
    version = _extraire_version(texte)
    assert version is not None
    assert "5.9.0" in version
    assert "no OCR" in version


def test_version_absente_si_rien_ne_correspond():
    assert _extraire_version("une sortie sans rapport\n") is None


def test_message_interligne_parle_de_la_photo():
    message = _message_echec(0, ["WARN  Interline value is too small: 7"])
    assert "photo" in message.lower()
    assert "Audiveris" in message


def test_message_sans_portee():
    message = _message_echec(0, ["WARN  No staff found in sheet"])
    assert "portée" in message.lower()


def test_message_memoire():
    message = _message_echec(1, ["java.lang.OutOfMemoryError: Java heap space"])
    assert "MPT_AUDIVERIS_MEMOIRE" in message


def test_message_tessdata():
    message = _message_echec(1, ["Could not find TESSDATA folder"])
    assert "TESSDATA_PREFIX" in message


def test_message_par_defaut_reprend_le_journal():
    message = _message_echec(2, ["ligne a", "ligne b"])
    assert "code 2" in message
    assert "ligne b" in message


if __name__ == "__main__":
    for nom, fonction in sorted(list(globals().items())):
        if nom.startswith("test_") and callable(fonction):
            fonction()
            print("ok", nom)
    print("tous les tests du moteur Audiveris passent")
