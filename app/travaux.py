"""File de travaux : un scan est long, la requête HTTP ne l'attend pas.

Une page prend de une à plusieurs minutes de calcul. Le client dépose son image,
reçoit un identifiant, puis interroge l'état. Le serveur ne traite qu'un travail à
la fois par défaut : la reconnaissance sature déjà le processeur.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .analyse import analyser
from .config import config
from .image import ImageInvalide, preparer
from .moteurs import EchecMoteur, moteur

logger = logging.getLogger(__name__)

EN_ATTENTE = "en_attente"
EN_COURS = "en_cours"
TERMINE = "termine"
ECHOUE = "echoue"


class FileSaturee(Exception):
    """Trop de travaux en attente."""


class TropDeRequetes(Exception):
    """Quota par adresse IP dépassé."""


def _maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Travail:
    id: str
    nom_fichier: str
    dossier: Path
    etat: str = EN_ATTENTE
    progression: int = 0
    etape: str = "En attente"
    cree_le: str = field(default_factory=_maintenant)
    termine_le: str | None = None
    erreur: str | None = None
    rapport: dict | None = None
    remarques: list[str] = field(default_factory=list)
    journal: list[str] = field(default_factory=list)
    musicxml: Path | None = None
    apercu: Path | None = None
    _cree_mono: float = field(default_factory=time.monotonic)

    def en_dict(self, position_file: int | None = None) -> dict:
        corps = {
            "id": self.id,
            "nomFichier": self.nom_fichier,
            "etat": self.etat,
            "progression": self.progression,
            "etape": self.etape,
            "creeLe": self.cree_le,
            "termineLe": self.termine_le,
            "erreur": self.erreur,
            "rapport": self.rapport,
            "remarques": self.remarques,
            "musicxml": f"/api/scans/{self.id}/musicxml" if self.musicxml else None,
            "apercu": f"/api/scans/{self.id}/apercu" if self.apercu else None,
        }
        if position_file is not None:
            corps["positionFile"] = position_file
        return corps


class Gestionnaire:
    def __init__(self) -> None:
        self._travaux: dict[str, Travail] = {}
        self._verrou = threading.Lock()
        self._executeur = ThreadPoolExecutor(
            max_workers=max(1, config.travaux_simultanes),
            thread_name_prefix="omr",
        )
        self._requetes_ip: dict[str, list[float]] = {}
        self._menage_lance = False

    # --- quota ------------------------------------------------------------

    def verifier_quota(self, ip: str) -> None:
        if config.limite_ip_par_heure <= 0:
            return
        limite = time.monotonic() - 3600
        with self._verrou:
            horodatages = [t for t in self._requetes_ip.get(ip, []) if t > limite]
            if len(horodatages) >= config.limite_ip_par_heure:
                self._requetes_ip[ip] = horodatages
                raise TropDeRequetes(
                    f"Limite de {config.limite_ip_par_heure} scans par heure atteinte."
                )
            horodatages.append(time.monotonic())
            self._requetes_ip[ip] = horodatages

    # --- cycle de vie -----------------------------------------------------

    def soumettre(self, donnees: bytes, nom_fichier: str) -> Travail:
        self._demarrer_menage()

        with self._verrou:
            en_file = sum(
                1 for t in self._travaux.values() if t.etat in (EN_ATTENTE, EN_COURS)
            )
            if en_file >= config.file_max:
                raise FileSaturee(
                    f"{en_file} scans déjà en file ; réessayez dans quelques minutes."
                )

        identifiant = uuid.uuid4().hex[:16]
        dossier = config.dossier_travaux / identifiant
        dossier.mkdir(parents=True, exist_ok=True)

        # Préparée tout de suite : un fichier illisible doit être refusé maintenant,
        # pas trois minutes plus tard au bout de la file.
        try:
            image = preparer(donnees, nom_fichier, dossier)
        except ImageInvalide:
            shutil.rmtree(dossier, ignore_errors=True)
            raise

        travail = Travail(id=identifiant, nom_fichier=nom_fichier, dossier=dossier)
        travail.remarques = list(image.remarques)

        with self._verrou:
            self._travaux[identifiant] = travail

        self._executeur.submit(self._traiter, travail, image.chemin)
        return travail

    def position_file(self, travail: Travail) -> int | None:
        if travail.etat != EN_ATTENTE:
            return None
        with self._verrou:
            attente = [
                t for t in self._travaux.values() if t.etat == EN_ATTENTE
            ]
        attente.sort(key=lambda t: t._cree_mono)
        for index, t in enumerate(attente):
            if t.id == travail.id:
                return index
        return None

    def obtenir(self, identifiant: str) -> Travail | None:
        with self._verrou:
            return self._travaux.get(identifiant)

    def supprimer(self, identifiant: str) -> bool:
        with self._verrou:
            travail = self._travaux.pop(identifiant, None)
        if travail is None:
            return False
        shutil.rmtree(travail.dossier, ignore_errors=True)
        return True

    def etat_global(self) -> dict:
        with self._verrou:
            valeurs = list(self._travaux.values())
        return {
            "enCours": sum(1 for t in valeurs if t.etat == EN_COURS),
            "enAttente": sum(1 for t in valeurs if t.etat == EN_ATTENTE),
            "total": len(valeurs),
        }

    # --- exécution --------------------------------------------------------

    def _traiter(self, travail: Travail, image: Path) -> None:
        travail.etat = EN_COURS
        travail.progression = 2
        travail.etape = "Préparation"

        def progression(pourcent: int, etape: str) -> None:
            travail.progression = max(travail.progression, min(pourcent, 99))
            travail.etape = etape

        sortie = travail.dossier / "sortie"
        try:
            resultat = moteur().transcrire(image, sortie, progression)
            contenu = resultat.musicxml.read_bytes()
            if resultat.musicxml.suffix.lower() == ".mxl":
                contenu = _extraire_mxl(contenu)
                destination = travail.dossier / "resultat.musicxml"
                destination.write_bytes(contenu)
                travail.musicxml = destination
            else:
                travail.musicxml = resultat.musicxml

            travail.rapport = analyser(contenu).en_dict()
            travail.apercu = resultat.apercu
            travail.journal = resultat.journal
            travail.progression = 100
            travail.etape = "Terminé"
            travail.etat = TERMINE
        except (EchecMoteur, ImageInvalide) as erreur:
            travail.etat = ECHOUE
            travail.erreur = str(erreur)
            travail.etape = "Échec"
            logger.warning("Travail %s en échec : %s", travail.id, erreur)
        except Exception as erreur:  # imprévu : on ne perd pas le serveur
            travail.etat = ECHOUE
            travail.erreur = f"Erreur interne : {erreur}"
            travail.etape = "Échec"
            logger.exception("Travail %s : erreur inattendue", travail.id)
        finally:
            travail.termine_le = _maintenant()

    # --- ménage -----------------------------------------------------------

    def _demarrer_menage(self) -> None:
        if self._menage_lance:
            return
        self._menage_lance = True
        fil = threading.Thread(target=self._boucle_menage, daemon=True, name="menage")
        fil.start()

    def _boucle_menage(self) -> None:
        while True:
            time.sleep(300)
            try:
                self.faire_le_menage()
            except Exception:  # pragma: no cover
                logger.exception("Ménage : échec")

    def faire_le_menage(self) -> int:
        limite = time.monotonic() - config.duree_vie_min * 60
        with self._verrou:
            perimes = [
                t
                for t in self._travaux.values()
                if t._cree_mono < limite and t.etat in (TERMINE, ECHOUE)
            ]
            for travail in perimes:
                self._travaux.pop(travail.id, None)
        for travail in perimes:
            shutil.rmtree(travail.dossier, ignore_errors=True)
        if perimes:
            logger.info("Ménage : %d travaux effacés", len(perimes))
        return len(perimes)


def _extraire_mxl(donnees: bytes) -> bytes:
    """Un .mxl est un zip ; on en tire le MusicXML principal."""
    import io
    import zipfile
    from xml.etree import ElementTree as ET

    with zipfile.ZipFile(io.BytesIO(donnees)) as archive:
        chemin = None
        if "META-INF/container.xml" in archive.namelist():
            racine = ET.fromstring(archive.read("META-INF/container.xml"))
            element = racine.find(".//rootfile")
            if element is not None:
                chemin = element.get("full-path")
        if chemin is None:
            candidats = [
                n
                for n in archive.namelist()
                if n.lower().endswith((".xml", ".musicxml")) and not n.startswith("META-INF")
            ]
            if not candidats:
                raise EchecMoteur("Archive .mxl sans MusicXML.")
            chemin = candidats[0]
        return archive.read(chemin)


gestionnaire = Gestionnaire()
