"""`ColorPalette`: paleta con rol por color y **contraste medido**
(tool-surface.md §6: "paleta" en lo que el propietario aporta;
`data-model.md` no modelaba esto todavia). El contraste no se calcula aqui
-- llega ya medido (herramienta externa u ojo humano) y el dominio solo
verifica el umbral WCAG AA, igual que `creative.domain.brand_kit` valida
`#RRGGBB` pero sin depender de ese contexto (grafo aciclico, plan.md §4)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from safent_ads.brand.domain.errors import InvalidContrastRatioError, InvalidHexColorError

_HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
_MIN_CONTRAST_RATIO = 1.0
_MAX_CONTRAST_RATIO = 21.0
_WCAG_AA_NORMAL_TEXT_THRESHOLD = 4.5


class ColorRole(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    ACCENT = "accent"
    BACKGROUND = "background"
    TEXT = "text"


@dataclass(frozen=True, slots=True, kw_only=True)
class ColorSwatch:
    role: ColorRole
    hex: str
    contrast_ratio_on_white: float

    def __post_init__(self) -> None:
        if not _HEX_COLOR_PATTERN.match(self.hex):
            raise InvalidHexColorError(f"hex invalido, se esperaba #RRGGBB: {self.hex!r}")
        if not _MIN_CONTRAST_RATIO <= self.contrast_ratio_on_white <= _MAX_CONTRAST_RATIO:
            raise InvalidContrastRatioError(
                f"contrast_ratio_on_white fuera de [{_MIN_CONTRAST_RATIO}, "
                f"{_MAX_CONTRAST_RATIO}]: {self.contrast_ratio_on_white!r}"
            )

    def meets_wcag_aa_normal_text(self) -> bool:
        return self.contrast_ratio_on_white >= _WCAG_AA_NORMAL_TEXT_THRESHOLD


@dataclass(frozen=True, slots=True, kw_only=True)
class ColorPalette:
    swatches: tuple[ColorSwatch, ...] = ()

    def swatch_for(self, role: ColorRole) -> ColorSwatch | None:
        return next((s for s in self.swatches if s.role == role), None)
