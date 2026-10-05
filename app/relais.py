"""Relais de partition entre deux appareils, en mémoire et pour trois minutes.

Le site sait déjà transférer une partition d'un appareil à l'autre par un
`RTCDataChannel` direct, appairé en se montrant deux QR codes. C'est autonome,
mais cela demande deux scans, une caméra, un contexte HTTPS et le même réseau
local. Quand ce service est joignable, un dépôt suffit : l'émetteur dépose, le
receveur tape quatre chiffres.

**Ce module ne persiste rien.** Pas de fichier, pas de dossier de travaux, pas
de volume Docker. Une enveloppe vit en mémoire, dans un dictionnaire, et en
disparaît au premier des trois évènements suivants :

1. le receveur la récupère — elle est à **usage unique** ;
2. l'émetteur l'annule ;
3. le délai de `MPT_RELAIS_TTL_S` secondes (180 par défaut) s'écoule.

L'échéance est portée par un `threading.Timer` propre à chaque enveloppe, donc
l'effacement tombe à l'heure dite et non au prochain passage d'une boucle de
ménage. Une purge paresseuse double la minuterie à chaque accès : même si un
timer était perdu, rien de périmé ne peut être servi.

## Ce que le relais ne protège pas

Le service voit la partition en clair. Il n'y a pas de chiffrement de bout en
bout : avec un secret de quatre chiffres, une clé dérivée se casserait hors
ligne en quelques millisecondes, et un secret plus long annulerait justement le
confort qu'on cherche. Le chemin WebRTC, lui, reste chiffré de bout en bout par
DTLS — c'est une raison de le garder, au-delà du repli hors ligne.

Ce que le relais protège, en revanche :

- **l'énumération des codes** : le `GET` sur un code inexistant compte comme un
  essai, et une adresse IP n'en a que `MPT_RELAIS_ESSAIS_PAR_HEURE` ;
- **le vol silencieux** : l'usage unique fait que si quelqu'un devine le code le
  premier, le vrai receveur reçoit un 404 — l'incident se voit au lieu de passer
  inaperçu ;
- **le squat de codes** : un dépôt est soumis à son propre quota par IP, et le
  nombre comme le volume des enveloppes en attente sont plafonnés ;
- **l'oracle d'existence** : l'émetteur suit son dépôt par un **jeton** de 32
  caractères rendu par le dépôt, jamais par le code. Sans cela, la route de
  suivi dirait à n'importe qui si un code est pris, ce qui réduirait la
  recherche à un balayage sans limite.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

from .config import config

logger = logging.getLogger(__name__)

EN_ATTENTE = "en_attente"
RECUPEREE = "recuperee"
EXPIREE = "expiree"
ANNULEE = "annulee"

#: Un code est exactement quatre chiffres. C'est le client qui le tire au sort —
#: `crypto.getRandomValues` côté navigateur — et qui réessaie avec un autre si
#: celui-ci est déjà pris.
LONGUEUR_CODE = 4


class CodeInvalide(Exception):
    """Le code n'est pas quatre chiffres."""


class CodeOccupe(Exception):
    """Une partition attend déjà sous ce code."""


class RelaisSature(Exception):
    """Trop d'enveloppes, ou trop d'octets, en attente."""


class TropDEssais(Exception):
    """Quota d'essais ou de dépôts dépassé pour cette adresse."""


class RelaisInactif(Exception):
    """Le relais est désactivé par la configuration."""


def code_valide(code: str) -> bool:
    return len(code) == LONGUEUR_CODE and code.isdigit()


@dataclass
class Enveloppe:
    code: str
    jeton: str
    #: `bytearray` et non `bytes` : on peut le remettre à zéro à l'effacement.
    #: Ce n'est pas un effacement sûr au sens cryptographique — la copie rendue
    #: au receveur est laissée au ramasse-miettes — mais la copie conservée par
    #: le service, elle, cesse réellement d'exister.
    donnees: bytearray
    nom: str
    expire_a: float
    cree_mono: float = field(default_factory=time.monotonic)
    minuterie: threading.Timer | None = None

    @property
    def octets(self) -> int:
        return len(self.donnees)

    def reste_s(self) -> int:
        return max(0, int(round(self.expire_a - time.monotonic())))

    def effacer(self) -> None:
        if self.minuterie is not None:
            self.minuterie.cancel()
            self.minuterie = None
        if self.donnees:
            self.donnees[:] = b"\x00" * len(self.donnees)
            self.donnees.clear()


@dataclass
class Suivi:
    """Ce qu'il reste d'une enveloppe partie : son sort, et rien de son contenu.

    L'émetteur a besoin de savoir si sa partition a bien été prise. Garder le
    jeton et un mot suffit, et ce mot ne révèle rien à qui n'a pas le jeton.
    """

    etat: str
    octets: int
    expire_a: float


class Relais:
    def __init__(self) -> None:
        self._enveloppes: dict[str, Enveloppe] = {}
        #: jeton → enveloppe en attente, ou suivi d'une enveloppe partie.
        self._jetons: dict[str, Enveloppe] = {}
        self._suivis: dict[str, Suivi] = {}
        self._verrou = threading.RLock()
        self._essais_ip: dict[str, list[float]] = {}
        self._depots_ip: dict[str, list[float]] = {}

    # --- quotas -----------------------------------------------------------

    def _compter(self, table: dict[str, list[float]], ip: str, limite: int, message: str) -> None:
        if limite <= 0:
            return
        plancher = time.monotonic() - 3600
        with self._verrou:
            horodatages = [t for t in table.get(ip, []) if t > plancher]
            if len(horodatages) >= limite:
                table[ip] = horodatages
                raise TropDEssais(message)
            horodatages.append(time.monotonic())
            table[ip] = horodatages

    # --- dépôt ------------------------------------------------------------

    def deposer(self, code: str, donnees: bytes, nom: str, ip: str) -> Enveloppe:
        if not config.relais_actif:
            raise RelaisInactif("Le relais est désactivé sur ce service.")
        if not code_valide(code):
            raise CodeInvalide("Le code doit être composé de quatre chiffres.")
        if not donnees:
            raise CodeInvalide("Dépôt vide.")
        if len(donnees) > config.relais_taille_max_octets:
            raise RelaisSature(
                f"Partition trop volumineuse : maximum {config.relais_taille_max_mo} Mo."
            )

        self._compter(
            self._depots_ip,
            ip,
            config.relais_depots_par_heure,
            f"Limite de {config.relais_depots_par_heure} partages par heure atteinte.",
        )

        with self._verrou:
            self._purger()
            if code in self._enveloppes:
                # Le client réessaie avec un autre code. C'est le seul cas où il
                # apprend quelque chose d'un code qu'il n'a pas choisi — et il
                # vient de le choisir, donc il n'apprend rien d'utile.
                raise CodeOccupe("Ce code est déjà pris ; un autre va être essayé.")
            if len(self._enveloppes) >= config.relais_max_attentes:
                raise RelaisSature("Trop de partages en cours ; réessayez dans trois minutes.")
            total = sum(e.octets for e in self._enveloppes.values())
            if total + len(donnees) > config.relais_octets_max_total:
                raise RelaisSature("Trop de partages en cours ; réessayez dans trois minutes.")

            enveloppe = Enveloppe(
                code=code,
                jeton=secrets.token_hex(16),
                donnees=bytearray(donnees),
                nom=nom[:120],
                expire_a=time.monotonic() + config.relais_ttl_s,
            )
            enveloppe.minuterie = threading.Timer(
                config.relais_ttl_s, self._expirer, args=(code, enveloppe.jeton)
            )
            enveloppe.minuterie.daemon = True
            enveloppe.minuterie.start()

            self._enveloppes[code] = enveloppe
            self._jetons[enveloppe.jeton] = enveloppe

        logger.info("Relais : dépôt sous le code %s (%d o)", code, len(donnees))
        return enveloppe

    # --- récupération -----------------------------------------------------

    def recuperer(self, code: str, ip: str) -> Enveloppe | None:
        """Rend l'enveloppe **et la retire** : un code ne sert qu'une fois.

        Un code introuvable compte comme un essai ; un code trouvé, non. Sans
        quoi un receveur qui se trompe une fois puis réussit serait puni pour
        rien, et surtout : seul l'échec renseigne un attaquant.
        """
        if not config.relais_actif:
            raise RelaisInactif("Le relais est désactivé sur ce service.")
        if not code_valide(code):
            raise CodeInvalide("Le code doit être composé de quatre chiffres.")

        with self._verrou:
            self._purger()
            enveloppe = self._enveloppes.get(code)
            if enveloppe is None:
                # Le quota est compté **hors** du verrou logique du dépôt mais
                # dans le même verrou réentrant : `_compter` le reprend sans se
                # bloquer (d'où `RLock`).
                self._compter(
                    self._essais_ip,
                    ip,
                    config.relais_essais_par_heure,
                    "Trop de codes erronés ; patientez avant de réessayer.",
                )
                return None

            self._enveloppes.pop(code, None)
            self._jetons.pop(enveloppe.jeton, None)
            self._suivis[enveloppe.jeton] = Suivi(
                etat=RECUPEREE, octets=enveloppe.octets, expire_a=enveloppe.expire_a
            )
            # La copie rendue est détachée avant l'effacement du tampon conservé.
            copie = bytes(enveloppe.donnees)
            nom = enveloppe.nom
            enveloppe.effacer()  # annule aussi la minuterie

        logger.info("Relais : code %s récupéré (%d o)", code, len(copie))
        return Enveloppe(
            code=code,
            jeton="",
            donnees=bytearray(copie),
            nom=nom,
            expire_a=time.monotonic(),
        )

    # --- suivi et annulation, par jeton ----------------------------------

    def suivre(self, jeton: str) -> dict | None:
        with self._verrou:
            self._purger()
            enveloppe = self._jetons.get(jeton)
            if enveloppe is not None:
                return {
                    "etat": EN_ATTENTE,
                    "octets": enveloppe.octets,
                    "resteS": enveloppe.reste_s(),
                }
            suivi = self._suivis.get(jeton)
            if suivi is not None:
                return {"etat": suivi.etat, "octets": suivi.octets, "resteS": 0}
        return None

    def annuler(self, jeton: str) -> bool:
        with self._verrou:
            enveloppe = self._jetons.pop(jeton, None)
            if enveloppe is None:
                self._purger()
                return jeton in self._suivis
            self._enveloppes.pop(enveloppe.code, None)
            self._suivis[jeton] = Suivi(
                etat=ANNULEE, octets=enveloppe.octets, expire_a=enveloppe.expire_a
            )
            enveloppe.effacer()
        logger.info("Relais : code %s annulé par l'émetteur", enveloppe.code)
        return True

    # --- péremption -------------------------------------------------------

    def _expirer(self, code: str, jeton: str) -> None:
        with self._verrou:
            enveloppe = self._enveloppes.get(code)
            if enveloppe is None or enveloppe.jeton != jeton:
                return  # déjà récupérée ou annulée, et le code a pu être réattribué
            self._enveloppes.pop(code, None)
            self._jetons.pop(jeton, None)
            self._suivis[jeton] = Suivi(
                etat=EXPIREE, octets=enveloppe.octets, expire_a=enveloppe.expire_a
            )
            enveloppe.effacer()
        logger.info("Relais : code %s périmé sans récupération", code)

    def _purger(self) -> None:
        """Double la minuterie. Appelée sous verrou, à chaque accès."""
        maintenant = time.monotonic()
        perimees = [e for e in self._enveloppes.values() if e.expire_a <= maintenant]
        for enveloppe in perimees:
            self._enveloppes.pop(enveloppe.code, None)
            self._jetons.pop(enveloppe.jeton, None)
            self._suivis[enveloppe.jeton] = Suivi(
                etat=EXPIREE, octets=enveloppe.octets, expire_a=enveloppe.expire_a
            )
            enveloppe.effacer()
        # Un suivi ne sert qu'à répondre à l'émetteur, qui a cessé d'attendre
        # bien avant. On garde deux fois le TTL, pas plus.
        plancher = maintenant - 2 * config.relais_ttl_s
        for jeton, suivi in list(self._suivis.items()):
            if suivi.expire_a < plancher:
                self._suivis.pop(jeton, None)

    # --- état global ------------------------------------------------------

    def etat_global(self) -> dict:
        with self._verrou:
            self._purger()
            return {
                "actif": config.relais_actif,
                "enAttente": len(self._enveloppes),
                "octets": sum(e.octets for e in self._enveloppes.values()),
                "ttlS": config.relais_ttl_s,
                "tailleMaxMo": config.relais_taille_max_mo,
            }

    def vider(self) -> int:
        """Pour les tests, et pour un arrêt propre."""
        with self._verrou:
            n = len(self._enveloppes)
            for enveloppe in list(self._enveloppes.values()):
                enveloppe.effacer()
            self._enveloppes.clear()
            self._jetons.clear()
            self._suivis.clear()
        return n


relais = Relais()
