"""API HTTP du service de reconnaissance de partitions.

Starlette plutôt que FastAPI : cinq routes, un seul corps multipart, aucun schéma
à valider. Cela évite une dépendance et une couche de modèles pour rien. Le
remplacement par FastAPI, si le besoin vient, ne touche que ce fichier.
"""

from __future__ import annotations

import logging

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from .config import config
from .image import HEIC_DISPONIBLE, PDF_DISPONIBLE, ImageInvalide
from .moteurs import moteur
from .travaux import FileSaturee, TropDeRequetes, gestionnaire

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s · %(message)s",
)
logger = logging.getLogger("mypianotool.omr")

VERSION = "0.1.0"
TYPE_MUSICXML = "application/vnd.recordare.musicxml+xml"


def _erreur(message: str, code: int, **extra) -> JSONResponse:
    return JSONResponse({"erreur": message, **extra}, status_code=code)


async def _vider(request: Request) -> None:
    """Consomme et jette le corps, sans jamais le garder en mémoire."""
    lus = 0
    plafond = config.taille_max_octets * 2
    try:
        async for morceau in request.stream():
            lus += len(morceau)
            if lus > plafond:
                break
    except Exception:
        pass


async def sante(request: Request) -> JSONResponse:
    m = moteur()
    return JSONResponse(
        {
            "etat": "ok",
            "version": VERSION,
            "moteur": m.nom,
            "moteurPret": m.pret(),
            # Renseignée par les moteurs qui savent se présenter (Audiveris).
            "moteurVersion": getattr(m, "version", lambda: None)(),
            "formats": {
                "heic": HEIC_DISPONIBLE,
                "pdf": PDF_DISPONIBLE,
            },
            "limites": {
                "tailleMaxMo": config.taille_max_mo,
                "fileMax": config.file_max,
                "scansParHeureParIp": config.limite_ip_par_heure,
                "dureeVieMin": config.duree_vie_min,
            },
            "travaux": gestionnaire.etat_global(),
        }
    )


async def creer_scan(request: Request) -> JSONResponse:
    ip = request.client.host if request.client else "inconnue"
    try:
        gestionnaire.verifier_quota(ip)
    except TropDeRequetes as erreur:
        return _erreur(str(erreur), 429)

    longueur = request.headers.get("content-length")
    if longueur is not None and longueur.isdigit() and int(longueur) > config.taille_max_octets:
        # Répondre sans lire couperait la connexion au milieu de l'envoi et le
        # navigateur n'afficherait qu'une « erreur réseau ». On vide le flux pour
        # que le 413 arrive vraiment jusqu'au client.
        await _vider(request)
        return _erreur(f"Fichier trop lourd : maximum {config.taille_max_mo} Mo.", 413)

    try:
        formulaire = await request.form()
    except Exception:
        return _erreur("Corps de requête invalide (multipart attendu).", 400)

    try:
        fichier = formulaire.get("image")
        if not isinstance(fichier, UploadFile):
            return _erreur("Champ « image » manquant.", 400)

        donnees = await fichier.read()
        if len(donnees) == 0:
            return _erreur("Fichier vide.", 400)
        if len(donnees) > config.taille_max_octets:
            return _erreur(f"Fichier trop lourd : maximum {config.taille_max_mo} Mo.", 413)
        nom = fichier.filename or "partition"
    finally:
        await formulaire.close()

    try:
        travail = await run_in_threadpool(gestionnaire.soumettre, donnees, nom)
    except ImageInvalide as erreur:
        return _erreur(str(erreur), 415)
    except FileSaturee as erreur:
        return _erreur(str(erreur), 503)

    logger.info("Scan %s déposé (%s, %.1f Mo)", travail.id, nom, len(donnees) / 1e6)
    return JSONResponse(
        travail.en_dict(position_file=gestionnaire.position_file(travail)),
        status_code=202,
        headers={"Location": f"/api/scans/{travail.id}"},
    )


async def etat_scan(request: Request) -> JSONResponse:
    travail = gestionnaire.obtenir(request.path_params["identifiant"])
    if travail is None:
        return _erreur("Scan inconnu ou expiré.", 404)
    return JSONResponse(travail.en_dict(position_file=gestionnaire.position_file(travail)))


async def musicxml_scan(request: Request) -> Response:
    travail = gestionnaire.obtenir(request.path_params["identifiant"])
    if travail is None:
        return _erreur("Scan inconnu ou expiré.", 404)
    if travail.musicxml is None or not travail.musicxml.exists():
        return _erreur("Aucun MusicXML pour ce scan.", 409, etat=travail.etat)

    base = travail.nom_fichier.rsplit(".", 1)[0] or "partition"
    return FileResponse(
        travail.musicxml,
        media_type=TYPE_MUSICXML,
        filename=f"{base}.musicxml",
    )


async def apercu_scan(request: Request) -> Response:
    travail = gestionnaire.obtenir(request.path_params["identifiant"])
    if travail is None:
        return _erreur("Scan inconnu ou expiré.", 404)
    if travail.apercu is None or not travail.apercu.exists():
        return _erreur("Aucun aperçu pour ce scan.", 404)
    return FileResponse(travail.apercu, media_type="image/png")


async def supprimer_scan(request: Request) -> Response:
    if gestionnaire.supprimer(request.path_params["identifiant"]):
        return Response(status_code=204)
    return _erreur("Scan inconnu ou expiré.", 404)


routes = [
    Route("/api/sante", sante, methods=["GET"]),
    Route("/api/scans", creer_scan, methods=["POST"]),
    Route("/api/scans/{identifiant}", etat_scan, methods=["GET"]),
    Route("/api/scans/{identifiant}", supprimer_scan, methods=["DELETE"]),
    Route("/api/scans/{identifiant}/musicxml", musicxml_scan, methods=["GET"]),
    Route("/api/scans/{identifiant}/apercu", apercu_scan, methods=["GET"]),
]

middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=config.origines,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        max_age=3600,
    )
]


def creer_application() -> Starlette:
    config.dossier_travaux.mkdir(parents=True, exist_ok=True)
    application = Starlette(
        routes=routes,
        middleware=middleware,
        # Garde-fou de dernier recours : la vraie limite est contrôlée dans
        # `creer_scan`, qui sait répondre 413 proprement. Celle-ci n'est là que
        # pour un client qui mentirait sur sa taille, et elle coupe la connexion.
        max_body_size=config.taille_max_octets * 2,
    )
    logger.info(
        "Service prêt · moteur=%s prêt=%s · origines=%s · travaux=%s",
        moteur().nom,
        moteur().pret(),
        ", ".join(config.origines),
        config.dossier_travaux,
    )
    return application


app = creer_application()
