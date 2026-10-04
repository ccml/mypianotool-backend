#!/usr/bin/env python3
"""Télécharge les poids d'oemer, que `pip install oemer` n'embarque pas.

Sans eux, oemer les récupère tout seul au premier scan — soit plusieurs minutes
d'attente pour le premier utilisateur, et un échec si la machine n'a pas accès à
GitHub. On les met donc en place à la construction de l'image Docker.

    python scripts/telecharger-modeles.py          # modèles ONNX (par défaut)
    python scripts/telecharger-modeles.py --tf     # ajoute les poids TensorFlow
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

BASE = "https://github.com/BreezeWhite/oemer/releases/download/checkpoints"

# (nom de l'asset, dossier de destination, nom final) — le préfixe saute.
FICHIERS_ONNX = [
    ("1st_model.onnx", "unet_big", "model.onnx"),
    ("2nd_model.onnx", "seg_net", "model.onnx"),
]
FICHIERS_TF = [
    ("1st_weights.h5", "unet_big", "weights.h5"),
    ("2nd_weights.h5", "seg_net", "weights.h5"),
]


def dossier_checkpoints() -> Path:
    try:
        import oemer
    except ImportError:
        print("oemer n'est pas installé (pip install oemer).", file=sys.stderr)
        raise SystemExit(2)
    return Path(oemer.__file__).parent / "checkpoints"


def telecharger(asset: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        print(f"  déjà là : {destination}")
        return
    url = f"{BASE}/{asset}"
    print(f"  {url}\n    → {destination}")
    temporaire = destination.with_suffix(destination.suffix + ".partiel")
    with urllib.request.urlopen(url, timeout=600) as reponse, temporaire.open("wb") as sortie:
        shutil.copyfileobj(reponse, sortie, length=1024 * 1024)
    temporaire.replace(destination)
    print(f"    {destination.stat().st_size / 1e6:.1f} Mo")


def main() -> int:
    analyseur = argparse.ArgumentParser(description=__doc__)
    analyseur.add_argument(
        "--tf",
        action="store_true",
        help="télécharge aussi les poids TensorFlow (nécessaires seulement avec --use-tf)",
    )
    arguments = analyseur.parse_args()

    racine = dossier_checkpoints()
    print(f"Modèles oemer dans {racine}")

    fichiers = list(FICHIERS_ONNX) + (list(FICHIERS_TF) if arguments.tf else [])
    for asset, sous_dossier, nom in fichiers:
        telecharger(asset, racine / sous_dossier / nom)

    print("Terminé.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
