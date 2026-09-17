"""`ToneOfVoice`: descripcion mas adjetivos y palabras a evitar. Estructura
minima para que `spanish-ad-copy`/`check_ai_tone` (tool-surface.md §2.6,
fuera de este lane) tengan algo mas que un parrafo suelto sobre el que
juzgar un borrador."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.brand.domain.errors import BlankFieldError


@dataclass(frozen=True, slots=True, kw_only=True)
class ToneOfVoice:
    description: str
    adjectives: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise BlankFieldError("description de tono de voz vacia")
