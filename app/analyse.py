"""Relecture du MusicXML produit par le moteur, pour en tirer un compte rendu.

Un moteur d'OMR ne donne pas de score de confiance exploitable. En revanche, ses
erreurs laissent une trace mesurable : une mesure dont les durées ne tombent pas
juste est presque toujours une mesure mal lue. C'est ce que l'on remonte, pour que
la page Partition puisse pointer l'utilisateur là où regarder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

# Au delà, la liste n'aide plus personne : c'est toute la page qui est à revoir.
MAX_MESURES_SIGNALEES = 40


@dataclass
class Rapport:
    parties: int = 0
    mesures: int = 0
    notes: int = 0
    accords: int = 0
    silences: int = 0
    liaisons: int = 0
    alterations: int = 0
    chiffrage: str | None = None
    armure: int | None = None
    mesures_suspectes: list[int] = field(default_factory=list)
    mesures_suspectes_total: int = 0
    avertissements: list[str] = field(default_factory=list)

    def en_dict(self) -> dict:
        return {
            "parties": self.parties,
            "mesures": self.mesures,
            "notes": self.notes,
            "accords": self.accords,
            "silences": self.silences,
            "liaisons": self.liaisons,
            "alterations": self.alterations,
            "chiffrage": self.chiffrage,
            "armure": self.armure,
            "mesuresSuspectes": self.mesures_suspectes,
            "mesuresSuspectesTotal": self.mesures_suspectes_total,
            "avertissements": self.avertissements,
        }


def _entier(element: ET.Element | None, defaut: int = 0) -> int:
    if element is None or element.text is None:
        return defaut
    try:
        return int(float(element.text.strip()))
    except ValueError:
        return defaut


def _duree_attendue(divisions: int, beats: int, beat_type: int) -> int:
    """Durée d'une mesure pleine, en unités `divisions` (divisions = une noire)."""
    if beat_type <= 0:
        return 0
    return round(divisions * 4 * beats / beat_type)


def analyser(musicxml: bytes | str) -> Rapport:
    rapport = Rapport()

    try:
        racine = ET.fromstring(musicxml if isinstance(musicxml, bytes) else musicxml.encode())
    except ET.ParseError as erreur:
        rapport.avertissements.append(f"MusicXML illisible : {erreur}")
        return rapport

    if racine.tag == "score-timewise":
        rapport.avertissements.append(
            "MusicXML au format timewise : le site attend du partwise."
        )
        return rapport

    parties = racine.findall("part")
    rapport.parties = len(parties)
    if not parties:
        rapport.avertissements.append("Aucune partie dans le fichier.")
        return rapport

    suspectes: set[int] = set()
    mesures_par_partie: list[int] = []

    for partie in parties:
        divisions = 1
        beats, beat_type = 0, 0
        mesures_partie = partie.findall("measure")
        mesures_par_partie.append(len(mesures_partie))

        for index, mesure in enumerate(mesures_partie, start=1):
            numero = _numero_mesure(mesure, index)

            attributs = mesure.find("attributes")
            if attributs is not None:
                divisions = _entier(attributs.find("divisions"), divisions) or 1
                temps = attributs.find("time")
                if temps is not None:
                    beats = _entier(temps.find("beats"), beats)
                    beat_type = _entier(temps.find("beat-type"), beat_type)
                    if rapport.chiffrage is None and beats and beat_type:
                        rapport.chiffrage = f"{beats}/{beat_type}"
                cle = attributs.find("key")
                if cle is not None and rapport.armure is None:
                    rapport.armure = _entier(cle.find("fifths"), 0)

            position = 0
            position_max = 0

            for enfant in mesure:
                if enfant.tag == "note":
                    duree = _entier(enfant.find("duration"), 0)
                    accord = enfant.find("chord") is not None
                    silence = enfant.find("rest") is not None
                    cachee = enfant.get("print-object") == "no"

                    if silence:
                        if not cachee:
                            rapport.silences += 1
                    else:
                        rapport.notes += 1
                        if accord:
                            rapport.accords += 1
                        if enfant.find("accidental") is not None:
                            rapport.alterations += 1
                    if enfant.find("tie") is not None:
                        rapport.liaisons += 1

                    if not accord:
                        position += duree
                        position_max = max(position_max, position)
                elif enfant.tag == "backup":
                    position -= _entier(enfant.find("duration"), 0)
                elif enfant.tag == "forward":
                    position += _entier(enfant.find("duration"), 0)
                    position_max = max(position_max, position)

            attendue = _duree_attendue(divisions, beats, beat_type)
            if attendue > 0 and position_max != attendue:
                # Une levée est normale en première mesure, on ne la compte pas.
                premiere = index == 1 and position_max < attendue
                if not premiere:
                    suspectes.add(numero)

    # Une partition de piano fait deux parties ; le nombre de mesures du morceau
    # est celui de la portée la plus longue, pas la somme des deux.
    rapport.mesures = max(mesures_par_partie, default=0)
    ordonnees = sorted(suspectes)
    rapport.mesures_suspectes_total = len(ordonnees)
    rapport.mesures_suspectes = ordonnees[:MAX_MESURES_SIGNALEES]

    if rapport.notes == 0:
        rapport.avertissements.append(
            "Aucune note reconnue : photo trop floue, trop sombre, ou cadrée de travers."
        )
    if beats == 0:
        rapport.avertissements.append("Aucun chiffrage de mesure trouvé.")
    if rapport.mesures_suspectes_total:
        part = rapport.mesures_suspectes_total / max(rapport.mesures, 1)
        if part > 0.5:
            rapport.avertissements.append(
                "Plus de la moitié des mesures ne tombent pas juste : "
                "la reconnaissance a probablement échoué."
            )

    return rapport


def _numero_mesure(mesure: ET.Element, defaut: int) -> int:
    brut = mesure.get("number")
    if brut is None:
        return defaut
    try:
        return int(brut)
    except ValueError:
        return defaut
