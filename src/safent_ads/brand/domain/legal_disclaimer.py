"""`LegalDisclaimer`: aviso legal obligatorio en cierto tipo de creatividad
(p.ej. "Resultados de campañas anteriores, no garantia de resultados
futuros"). `applies_to=None` significa "todas las plataformas conectadas"."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.brand.domain.errors import BlankFieldError
from safent_ads.shared.ids import PlatformCode


@dataclass(frozen=True, slots=True, kw_only=True)
class LegalDisclaimer:
    text: str
    applies_to: tuple[PlatformCode, ...] | None = None

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise BlankFieldError("text de aviso legal vacio")

    def applies_to_platform(self, platform: PlatformCode) -> bool:
        return self.applies_to is None or platform in self.applies_to
