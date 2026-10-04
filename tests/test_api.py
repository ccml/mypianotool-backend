"""Tests de l'API, contre un vrai serveur uvicorn lancé en sous-processus.

Pas de client de test en mémoire : on veut vérifier le multipart, les codes de
statut et les en-têtes tels qu'ils sortent du serveur, puisque c'est ce que le
navigateur verra.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

from PIL import Image  # noqa: E402

PORT = int(os.environ.get("MPT_TEST_PORT", "8099"))
BASE = f"http://127.0.0.1:{PORT}"


# --- utilitaires ----------------------------------------------------------


def photo(largeur: int = 1600, hauteur: int = 1200) -> bytes:
    image = Image.new("RGB", (largeur, hauteur), "white")
    tampon = io.BytesIO()
    image.save(tampon, format="JPEG")
    return tampon.getvalue()


def multipart(nom_champ: str, nom_fichier: str, contenu: bytes) -> tuple[bytes, str]:
    frontiere = uuid.uuid4().hex
    corps = (
        f"--{frontiere}\r\n"
        f'Content-Disposition: form-data; name="{nom_champ}"; filename="{nom_fichier}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + contenu + f"\r\n--{frontiere}--\r\n".encode()
    return corps, f"multipart/form-data; boundary={frontiere}"


def requete(methode: str, chemin: str, corps: bytes | None = None, type_contenu: str | None = None):
    demande = urllib.request.Request(BASE + chemin, data=corps, method=methode)
    if type_contenu:
        demande.add_header("Content-Type", type_contenu)
    try:
        with urllib.request.urlopen(demande, timeout=30) as reponse:
            return reponse.status, reponse.read(), _entetes(reponse.headers)
    except urllib.error.HTTPError as erreur:
        return erreur.code, erreur.read(), _entetes(erreur.headers)


def _entetes(bruts) -> dict:
    """Les en-têtes HTTP/1.1 arrivent en minuscules ; on normalise une fois."""
    return {cle.lower(): valeur for cle, valeur in bruts.items()}


def json_de(brut: bytes) -> dict:
    return json.loads(brut.decode())


def deposer(nom: str = "partition.jpg", contenu: bytes | None = None):
    corps, type_contenu = multipart("image", nom, contenu if contenu is not None else photo())
    return requete("POST", "/api/scans", corps, type_contenu)


def attendre_fin(identifiant: str, limite_s: float = 30) -> dict:
    debut = time.monotonic()
    while time.monotonic() - debut < limite_s:
        statut, brut, _ = requete("GET", f"/api/scans/{identifiant}")
        assert statut == 200, (statut, brut)
        corps = json_de(brut)
        if corps["etat"] in ("termine", "echoue"):
            return corps
        time.sleep(0.2)
    raise AssertionError("le scan ne s'est pas terminé à temps")


# --- tests ----------------------------------------------------------------


def test_sante():
    statut, brut, _ = requete("GET", "/api/sante")
    assert statut == 200
    corps = json_de(brut)
    assert corps["etat"] == "ok"
    assert corps["moteur"] == "factice"
    assert corps["moteurPret"] is True
    assert corps["limites"]["tailleMaxMo"] > 0


def test_scan_complet():
    statut, brut, entetes = deposer()
    assert statut == 202, (statut, brut)
    depose = json_de(brut)
    assert depose["etat"] in ("en_attente", "en_cours")
    assert entetes["location"].endswith(depose["id"])

    fini = attendre_fin(depose["id"])
    assert fini["etat"] == "termine", fini
    assert fini["progression"] == 100
    assert fini["rapport"]["mesures"] == 4
    assert fini["rapport"]["mesuresSuspectes"] == [3]
    assert fini["musicxml"] == f"/api/scans/{depose['id']}/musicxml"

    statut, contenu, entetes = requete("GET", fini["musicxml"])
    assert statut == 200
    assert entetes["content-type"].startswith("application/vnd.recordare.musicxml")
    assert b"score-partwise" in contenu
    assert "partition.musicxml" in entetes["content-disposition"]


def test_apercu_disponible():
    statut, brut, _ = deposer()
    identifiant = json_de(brut)["id"]
    fini = attendre_fin(identifiant)
    assert fini["apercu"]
    statut, contenu, entetes = requete("GET", fini["apercu"])
    assert statut == 200
    assert entetes["content-type"] == "image/png"
    assert len(contenu) > 0


def test_suppression():
    statut, brut, _ = deposer()
    identifiant = json_de(brut)["id"]
    attendre_fin(identifiant)
    statut, _, _ = requete("DELETE", f"/api/scans/{identifiant}")
    assert statut == 204
    statut, _, _ = requete("GET", f"/api/scans/{identifiant}")
    assert statut == 404


def test_scan_inconnu():
    statut, brut, _ = requete("GET", "/api/scans/ffffffffffffffff")
    assert statut == 404
    assert "erreur" in json_de(brut)


def test_champ_image_manquant():
    corps, type_contenu = multipart("fichier", "partition.jpg", photo())
    statut, brut, _ = requete("POST", "/api/scans", corps, type_contenu)
    assert statut == 400
    assert "image" in json_de(brut)["erreur"]


def test_fichier_illisible_refuse_tout_de_suite():
    statut, brut, _ = deposer("note.txt", b"ceci n'est pas une partition")
    assert statut == 415
    assert "erreur" in json_de(brut)


def test_image_minuscule_refusee():
    statut, brut, _ = deposer("vignette.png", photo(120, 90))
    assert statut == 415
    assert "trop petite" in json_de(brut)["erreur"]


def test_fichier_vide_refuse():
    statut, brut, _ = deposer("vide.jpg", b"")
    assert statut == 400


def test_fichier_trop_lourd_refuse():
    corps, type_contenu = multipart("image", "enorme.jpg", b"0" * (26 * 1024 * 1024))
    statut, brut, _ = requete("POST", "/api/scans", corps, type_contenu)
    assert statut == 413, statut
    assert "Mo" in json_de(brut)["erreur"]


def test_cors_prevol():
    demande = urllib.request.Request(BASE + "/api/scans", method="OPTIONS")
    demande.add_header("Origin", "http://localhost:4200")
    demande.add_header("Access-Control-Request-Method", "POST")
    with urllib.request.urlopen(demande, timeout=10) as reponse:
        entetes = dict(reponse.headers)
    assert entetes.get("access-control-allow-origin") == "http://localhost:4200"


def test_origine_etrangere_refusee():
    demande = urllib.request.Request(BASE + "/api/sante", method="GET")
    demande.add_header("Origin", "https://exemple-pirate.test")
    with urllib.request.urlopen(demande, timeout=10) as reponse:
        entetes = dict(reponse.headers)
    assert "access-control-allow-origin" not in {k.lower() for k in entetes}


def test_quota_par_ip():
    # Le serveur de test tourne avec une limite basse (voir lancer_serveur).
    refuse = False
    for _ in range(25):
        statut, _, _ = deposer()
        if statut == 429:
            refuse = True
            break
    assert refuse, "le quota par IP n'a jamais déclenché"


# --- exécution ------------------------------------------------------------


def lancer_serveur(dossier: str) -> subprocess.Popen:
    environnement = dict(os.environ)
    environnement.update(
        {
            "MPT_MOTEUR": "factice",
            "MPT_FACTICE_DUREE_S": "0.4",
            "MPT_DOSSIER_TRAVAUX": dossier,
            "MPT_LIMITE_IP_PAR_HEURE": "15",
            "MPT_FILE_MAX": "20",
            "PYTHONPATH": str(RACINE),
        }
    )
    processus = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(RACINE),
        env=environnement,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    for _ in range(100):
        time.sleep(0.2)
        if processus.poll() is not None:
            raise AssertionError("le serveur s'est arrêté :\n" + (processus.stdout.read() if processus.stdout else ""))
        try:
            statut, _, _ = requete("GET", "/api/sante")
            if statut == 200:
                return processus
        except Exception:
            continue
    raise AssertionError("le serveur n'a pas démarré")


def main() -> int:
    with tempfile.TemporaryDirectory() as dossier:
        serveur = lancer_serveur(dossier)
        echecs = 0
        try:
            tests = [
                (nom, fonction)
                for nom, fonction in sorted(globals().items())
                if nom.startswith("test_") and callable(fonction)
            ]
            # Le quota passe en dernier : il consomme le reste des jetons.
            tests.sort(key=lambda couple: couple[0] == "test_quota_par_ip")
            for nom, fonction in tests:
                try:
                    fonction()
                    print("ok  ", nom)
                except Exception as erreur:
                    echecs += 1
                    print("ÉCHEC", nom, "→", erreur)
        finally:
            serveur.terminate()
            serveur.wait(timeout=10)
        print("—", "tout passe" if echecs == 0 else f"{echecs} échec(s)")
        return 1 if echecs else 0


if __name__ == "__main__":
    raise SystemExit(main())
