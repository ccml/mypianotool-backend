"""Tests du relais de partage.

Deux parties, parce que deux choses différentes sont à prouver :

- **en direct sur la classe** `Relais`, ce qui demande de maîtriser le temps et
  la configuration : la péremption, l'usage unique, les plafonds, les quotas, et
  le fait que le tampon conservé est bien remis à zéro ;
- **par HTTP**, ce que le navigateur verra réellement : codes de statut,
  en-têtes, et le corps rendu à l'octet près.

Le serveur de la session (`conftest.py`) tourne avec les quotas du relais
désactivés : sans cela, un test qui éprouve un mauvais code ferait tomber en 429
le test suivant, et l'ordre des fichiers déciderait du résultat. Les quotas sont
donc éprouvés dans la partie en direct, où ils sont réglés expressément.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

from app.config import config  # noqa: E402
from app.relais import (  # noqa: E402
    ANNULEE,
    EN_ATTENTE,
    EXPIREE,
    RECUPEREE,
    CodeInvalide,
    CodeOccupe,
    Relais,
    RelaisInactif,
    RelaisSature,
    TropDEssais,
    code_valide,
)

PORT = int(os.environ.get("MPT_TEST_PORT", "8099"))
BASE = f"http://127.0.0.1:{PORT}"

CHARGE = b"\x00\x04{\"v\":1}" + b"partition comprimee" * 10


@contextmanager
def reglages(**valeurs):
    """`config` est un dataclass gelé : on contourne, et on remet en place."""
    anciens = {nom: getattr(config, nom) for nom in valeurs}
    for nom, valeur in valeurs.items():
        object.__setattr__(config, nom, valeur)
    try:
        yield
    finally:
        for nom, valeur in anciens.items():
            object.__setattr__(config, nom, valeur)


@pytest.fixture
def r():
    relais = Relais()
    yield relais
    relais.vider()


# =========================================================== en direct


def test_code_valide():
    assert code_valide("0000") and code_valide("4271")
    for mauvais in ("", "123", "12345", "12a4", " 123", "１２３４"):
        assert not code_valide(mauvais), mauvais


def test_depot_puis_recuperation_rend_les_octets_exacts(r):
    enveloppe = r.deposer("1234", CHARGE, "Hymne à la joie", "10.0.0.1")
    assert enveloppe.code == "1234"
    assert len(enveloppe.jeton) == 32
    assert enveloppe.reste_s() > 170

    recue = r.recuperer("1234", "10.0.0.2")
    assert bytes(recue.donnees) == CHARGE
    assert recue.nom == "Hymne à la joie"


def test_un_code_ne_sert_qu_une_fois(r):
    r.deposer("1234", CHARGE, "x", "10.0.0.1")
    assert r.recuperer("1234", "10.0.0.2") is not None
    # Le second appel ne trouve rien : c'est ce qui rend un vol visible, puisque
    # le vrai receveur obtient alors un 404 au lieu de la partition.
    assert r.recuperer("1234", "10.0.0.2") is None


def test_le_tampon_conserve_est_remis_a_zero(r):
    enveloppe = r.deposer("1234", CHARGE, "x", "10.0.0.1")
    tampon = enveloppe.donnees  # la référence du service, pas une copie
    recue = r.recuperer("1234", "10.0.0.2")
    assert bytes(recue.donnees) == CHARGE
    assert len(tampon) == 0, "le tampon conservé doit être vidé, pas seulement déréférencé"


def test_code_deja_pris_refuse(r):
    r.deposer("1234", CHARGE, "x", "10.0.0.1")
    with pytest.raises(CodeOccupe):
        r.deposer("1234", CHARGE, "y", "10.0.0.9")
    # Et le dépôt refusé n'a rien abîmé : le premier est toujours là, intact.
    assert bytes(r.recuperer("1234", "10.0.0.2").donnees) == CHARGE


def test_code_libere_est_reutilisable(r):
    r.deposer("1234", CHARGE, "x", "10.0.0.1")
    r.recuperer("1234", "10.0.0.2")
    r.deposer("1234", b"autre", "y", "10.0.0.1")
    assert bytes(r.recuperer("1234", "10.0.0.2").donnees) == b"autre"


def test_codes_invalides_refuses(r):
    for mauvais in ("", "123", "12345", "abcd"):
        with pytest.raises(CodeInvalide):
            r.deposer(mauvais, CHARGE, "x", "10.0.0.1")
    with pytest.raises(CodeInvalide):
        r.deposer("1234", b"", "x", "10.0.0.1")


def test_peremption_efface_vraiment(r):
    with reglages(relais_ttl_s=1):
        enveloppe = r.deposer("1234", CHARGE, "x", "10.0.0.1")
        tampon = enveloppe.donnees
        jeton = enveloppe.jeton
        assert r.suivre(jeton)["etat"] == EN_ATTENTE
        time.sleep(1.3)
        # La minuterie a fait son travail sans qu'on ait rien demandé : c'est
        # le point de la décision, « 180 secondes en mémoire » et pas « jusqu'au
        # prochain passage d'une boucle de ménage ».
        assert r.recuperer("1234", "10.0.0.2") is None
        assert len(tampon) == 0
        assert r.suivre(jeton)["etat"] == EXPIREE


def test_purge_paresseuse_double_la_minuterie(r):
    """Même minuterie perdue, rien de périmé n'est servi."""
    with reglages(relais_ttl_s=1):
        enveloppe = r.deposer("1234", CHARGE, "x", "10.0.0.1")
        enveloppe.minuterie.cancel()  # on sabote expressément
        time.sleep(1.2)
        assert r.recuperer("1234", "10.0.0.2") is None


def test_suivi_par_jeton(r):
    enveloppe = r.deposer("1234", CHARGE, "Canon", "10.0.0.1")
    suivi = r.suivre(enveloppe.jeton)
    assert suivi["etat"] == EN_ATTENTE
    assert suivi["octets"] == len(CHARGE)
    assert 170 < suivi["resteS"] <= 180

    r.recuperer("1234", "10.0.0.2")
    assert r.suivre(enveloppe.jeton)["etat"] == RECUPEREE
    assert r.suivre("jeton-qui-n-existe-pas") is None


def test_annulation_par_l_emetteur(r):
    enveloppe = r.deposer("1234", CHARGE, "x", "10.0.0.1")
    tampon = enveloppe.donnees
    assert r.annuler(enveloppe.jeton) is True
    assert len(tampon) == 0
    assert r.recuperer("1234", "10.0.0.2") is None
    assert r.suivre(enveloppe.jeton)["etat"] == ANNULEE
    assert r.annuler("inconnu") is False


def test_le_jeton_ne_donne_pas_la_partition(r):
    """Le suivi ne doit rien dire du contenu : il n'a pas à le connaître."""
    enveloppe = r.deposer("1234", CHARGE, "Secret", "10.0.0.1")
    suivi = r.suivre(enveloppe.jeton)
    assert set(suivi) == {"etat", "octets", "resteS"}


def test_plafond_de_taille(r):
    with reglages(relais_taille_max_mo=1):
        with pytest.raises(RelaisSature):
            r.deposer("1234", b"x" * (1024 * 1024 + 1), "x", "10.0.0.1")
        r.deposer("1234", b"x" * 1024, "x", "10.0.0.1")  # juste en dessous : passe


def test_plafond_du_nombre_d_attentes(r):
    with reglages(relais_max_attentes=3):
        for i in range(3):
            r.deposer(f"100{i}", CHARGE, "x", "10.0.0.1")
        with pytest.raises(RelaisSature):
            r.deposer("2000", CHARGE, "x", "10.0.0.1")
        # Une place se libère dès qu'un transfert aboutit.
        r.recuperer("1000", "10.0.0.2")
        r.deposer("2000", CHARGE, "x", "10.0.0.1")


def test_plafond_du_volume_total(r):
    with reglages(relais_octets_max_total=4000, relais_taille_max_mo=8):
        r.deposer("1000", b"x" * 3000, "x", "10.0.0.1")
        with pytest.raises(RelaisSature):
            r.deposer("1001", b"x" * 1500, "x", "10.0.0.1")


def test_quota_de_codes_errones(r):
    """Ce qui rend inexploitable le balayage des 10 000 codes."""
    with reglages(relais_essais_par_heure=5):
        for _ in range(5):
            assert r.recuperer("9999", "10.0.0.7") is None
        with pytest.raises(TropDEssais):
            r.recuperer("9998", "10.0.0.7")
        # Le quota est par adresse : un autre appareil n'est pas puni.
        assert r.recuperer("9997", "10.0.0.8") is None


def test_un_code_juste_ne_consomme_pas_le_quota(r):
    """Sinon un receveur maladroit serait bloqué après quelques fautes de frappe."""
    with reglages(relais_essais_par_heure=3):
        for i in range(10):
            r.deposer("1234", CHARGE, "x", "10.0.0.1")
            assert r.recuperer("1234", "10.0.0.7") is not None


def test_quota_de_depots(r):
    with reglages(relais_depots_par_heure=2):
        r.deposer("1000", CHARGE, "x", "10.0.0.7")
        r.deposer("1001", CHARGE, "x", "10.0.0.7")
        with pytest.raises(TropDEssais):
            r.deposer("1002", CHARGE, "x", "10.0.0.7")
        r.deposer("1003", CHARGE, "x", "10.0.0.8")


def test_relais_desactive(r):
    with reglages(relais_actif=False):
        with pytest.raises(RelaisInactif):
            r.deposer("1234", CHARGE, "x", "10.0.0.1")
        with pytest.raises(RelaisInactif):
            r.recuperer("1234", "10.0.0.2")


def test_etat_global(r):
    r.deposer("1234", CHARGE, "x", "10.0.0.1")
    etat = r.etat_global()
    assert etat["actif"] is True
    assert etat["enAttente"] == 1
    assert etat["octets"] == len(CHARGE)
    assert etat["ttlS"] == config.relais_ttl_s


def test_le_nom_est_tronque(r):
    enveloppe = r.deposer("1234", CHARGE, "T" * 500, "10.0.0.1")
    assert len(enveloppe.nom) == 120


def test_depots_concurrents_sur_le_meme_code(r):
    """Un seul doit l'emporter : c'est le verrou qui le garantit, pas la chance."""
    import threading

    resultats: list[str] = []
    barriere = threading.Barrier(8)

    def tenter(i: int) -> None:
        barriere.wait()
        try:
            r.deposer("1234", CHARGE, f"n{i}", "10.0.0.%d" % i)
            resultats.append("ok")
        except CodeOccupe:
            resultats.append("occupe")

    fils = [threading.Thread(target=tenter, args=(i,)) for i in range(8)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    assert resultats.count("ok") == 1
    assert resultats.count("occupe") == 7


# =========================================================== par HTTP


def requete(methode: str, chemin: str, corps: bytes | None = None, entetes: dict | None = None):
    demande = urllib.request.Request(
        BASE + chemin, data=corps, method=methode, headers=entetes or {}
    )
    try:
        with urllib.request.urlopen(demande, timeout=30) as reponse:
            return reponse.status, reponse.read(), dict(reponse.headers)
    except urllib.error.HTTPError as erreur:
        return erreur.code, erreur.read(), dict(erreur.headers)


def deposer_http(code: str, charge: bytes = CHARGE, nom: str = "Canon"):
    return requete(
        "POST",
        "/api/relais",
        charge,
        {"X-Code": code, "X-Nom": nom, "Content-Type": "application/octet-stream"},
    )


def test_http_sante_annonce_le_relais():
    import json

    statut, corps, _ = requete("GET", "/api/sante")
    assert statut == 200
    sante = json.loads(corps)
    assert sante["relais"]["actif"] is True
    assert sante["relais"]["ttlS"] == 180
    # Le relais ne dépend pas du moteur : le site doit pouvoir s'en servir même
    # si Audiveris n'est pas prêt, donc la clé est à part de `moteurPret`.
    assert "moteurPret" in sante


def test_http_aller_retour_complet():
    import json

    statut, corps, _ = deposer_http("3141", nom="Clair de lune")
    assert statut == 201
    depot = json.loads(corps)
    assert depot["code"] == "3141"
    assert len(depot["jeton"]) == 32
    assert depot["octets"] == len(CHARGE)
    assert 170 < depot["resteS"] <= 180

    statut, recu, entetes = requete("GET", "/api/relais/3141")
    assert statut == 200
    assert recu == CHARGE
    assert entetes.get("X-Nom") == "Clair de lune"
    assert entetes.get("Cache-Control") == "no-store"

    # Usage unique, vu du dehors.
    statut, _, _ = requete("GET", "/api/relais/3141")
    assert statut == 404

    # Et l'émetteur l'apprend par son jeton.
    statut, corps, _ = requete("GET", f"/api/relais/jeton/{depot['jeton']}")
    assert statut == 200
    assert json.loads(corps)["etat"] == "recuperee"


def test_http_code_occupe_est_un_409():
    try:
        assert deposer_http("3142")[0] == 201
        assert deposer_http("3142")[0] == 409
    finally:
        requete("GET", "/api/relais/3142")


def test_http_code_mal_forme_est_un_400():
    for mauvais in ("abc", "12345", "1"):
        assert deposer_http(mauvais)[0] == 400, mauvais
    assert requete("GET", "/api/relais/abcd")[0] == 400
    assert deposer_http("3143", b"")[0] == 400


def test_http_trop_volumineux_est_un_413():
    trop = b"x" * (config.relais_taille_max_octets + 1024)
    statut, _, _ = deposer_http("3144", trop)
    assert statut == 413


def test_http_annulation():
    import json

    depot = json.loads(deposer_http("3145")[1])
    assert requete("DELETE", f"/api/relais/jeton/{depot['jeton']}")[0] == 204
    assert requete("GET", "/api/relais/3145")[0] == 404
    assert json.loads(requete("GET", f"/api/relais/jeton/{depot['jeton']}")[1])["etat"] == "annulee"


def test_http_jeton_inconnu_est_un_404():
    assert requete("GET", "/api/relais/jeton/" + "0" * 32)[0] == 404
    assert requete("DELETE", "/api/relais/jeton/" + "0" * 32)[0] == 404


def test_http_rien_n_est_ecrit_sur_le_disque():
    """La promesse du module : le relais ne touche pas au dossier de travaux."""
    import json

    avant = sorted(p.name for p in config.dossier_travaux.iterdir())
    depot = json.loads(deposer_http("3146")[1])
    pendant = sorted(p.name for p in config.dossier_travaux.iterdir())
    assert pendant == avant
    requete("GET", "/api/relais/3146")
    assert sorted(p.name for p in config.dossier_travaux.iterdir()) == avant
    assert json.loads(requete("GET", f"/api/relais/jeton/{depot['jeton']}")[1])["etat"] == "recuperee"


def test_http_charge_binaire_quelconque():
    """Le relais est aveugle au contenu : il ne doit rien supposer des octets."""
    charge = bytes(range(256)) * 40
    assert deposer_http("3147", charge)[0] == 201
    statut, recu, _ = requete("GET", "/api/relais/3147")
    assert statut == 200
    assert recu == charge
