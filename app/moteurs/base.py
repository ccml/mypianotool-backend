"""Interface commune aux moteurs de reconnaissance."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol


class EchecMoteur(Exception):
    """Le moteur n'a pas pu produire de MusicXML."""


@dataclass
class ResultatMoteur:
    musicxml: Path
    apercu: Path | None = None
    journal: list[str] = field(default_factory=list)


# Appelé au fil du traitement : (progression 0-100, étape lisible).
Progression = Callable[[int, str], None]


class Moteur(Protocol):
    nom: str

    def pret(self) -> bool:
        """Le moteur est installé et ses modèles sont en place."""

    def transcrire(
        self,
        image: Path,
        dossier_sortie: Path,
        progression: Progression,
    ) -> ResultatMoteur: ...
