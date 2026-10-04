"""Tests de la préparation d'image."""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app.image import ImageInvalide, preparer  # noqa: E402


def _photo(largeur: int, hauteur: int, format_: str = "JPEG") -> bytes:
    image = Image.new("RGB", (largeur, hauteur), "white")
    tampon = io.BytesIO()
    image.save(tampon, format=format_)
    return tampon.getvalue()


def test_grande_photo_est_reduite():
    with tempfile.TemporaryDirectory() as dossier:
        resultat = preparer(_photo(4032, 3024), "IMG_0001.JPG", Path(dossier))
        # 3508 px : la hauteur d'une page A4 scannée à 300 dpi, la résolution
        # qu'Audiveris veut garder pour mesurer l'interligne.
        assert max(resultat.largeur, resultat.hauteur) == 3508
        assert resultat.chemin.exists()
        assert any("réduite" in r for r in resultat.remarques)


def test_petite_image_est_agrandie_et_signalee():
    with tempfile.TemporaryDirectory() as dossier:
        resultat = preparer(_photo(800, 600), "petite.png", Path(dossier))
        assert max(resultat.largeur, resultat.hauteur) == 1800
        assert any("peu fiable" in r for r in resultat.remarques)


def test_image_minuscule_refusee():
    with tempfile.TemporaryDirectory() as dossier:
        try:
            preparer(_photo(100, 80), "vignette.png", Path(dossier))
        except ImageInvalide as erreur:
            assert "trop petite" in str(erreur)
        else:
            raise AssertionError("une vignette devrait être refusée")


def test_fichier_non_image_refuse():
    with tempfile.TemporaryDirectory() as dossier:
        try:
            preparer(b"ceci n'est pas une image", "note.txt", Path(dossier))
        except ImageInvalide:
            pass
        else:
            raise AssertionError("un texte devrait être refusé")


def test_sortie_est_en_niveaux_de_gris():
    with tempfile.TemporaryDirectory() as dossier:
        resultat = preparer(_photo(1600, 1200), "scan.jpg", Path(dossier))
        with Image.open(resultat.chemin) as image:
            assert image.mode == "L"
            assert image.format == "PNG"


if __name__ == "__main__":
    for nom, fonction in sorted(list(globals().items())):
        if nom.startswith("test_") and callable(fonction):
            fonction()
            print("ok", nom)
    print("tous les tests d'image passent")
