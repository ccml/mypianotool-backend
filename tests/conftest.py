"""Démarre un serveur en moteur factice pour toute la session pytest.

Les tests d'API tapent sur un vrai serveur ; les autres n'en ont pas besoin mais
ne s'en portent pas plus mal.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_api import lancer_serveur  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def serveur():
    with tempfile.TemporaryDirectory() as dossier:
        processus = lancer_serveur(dossier)
        try:
            yield processus
        finally:
            processus.terminate()
            processus.wait(timeout=10)
