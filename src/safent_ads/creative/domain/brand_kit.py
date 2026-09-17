"""`BrandKit` y `SafeArea` (creative-port.md: `CreativeBrief.brand_kit`,
`Timeline.safe_area`). "El area segura y los limites de texto de cada
emplazamiento se validan en la composicion, no despues del render"."""

from __future__ import annotations

import re
from dataclasses import dataclass

from safent_ads.creative.domain.identifiers import AssetId

_HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
_MAX_MARGIN_FRACTION = 0.5


class InvalidHexColorError(ValueError):
    """Color fuera del formato `#RRGGBB`."""


class InvalidSafeAreaError(ValueError):
    """Margen de area segura fuera de rango o los margenes no dejan area util."""


def _validate_hex_color(value: str, *, field: str) -> str:
    if not _HEX_COLOR_PATTERN.match(value):
        raise InvalidHexColorError(f"{field} invalido, se esperaba #RRGGBB: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class SafeArea:
    """Margenes como fraccion (0-0.5) de ancho/alto reservados para que el
    contenido critico no quede tapado por overlays de la plataforma
    (subtitulos nativos, CTA, barra de progreso de Stories/Reels)."""

    top: float
    bottom: float
    left: float
    right: float

    def __post_init__(self) -> None:
        for name, value in (
            ("top", self.top),
            ("bottom", self.bottom),
            ("left", self.left),
            ("right", self.right),
        ):
            if not 0.0 <= value <= _MAX_MARGIN_FRACTION:
                raise InvalidSafeAreaError(f"{name}={value!r} fuera de [0, 0.5]")
        if self.top + self.bottom >= 1.0 or self.left + self.right >= 1.0:
            raise InvalidSafeAreaError("los margenes no dejan area util")

    @classmethod
    def none(cls) -> SafeArea:
        return cls(top=0.0, bottom=0.0, left=0.0, right=0.0)

    @classmethod
    def reels_default(cls) -> SafeArea:
        """Margenes conservadores para Reels/Stories 9:16: UI nativa arriba
        (perfil/musica) y abajo (caption/CTA)."""
        return cls(top=0.12, bottom=0.18, left=0.05, right=0.05)


@dataclass(frozen=True, slots=True)
class BrandKit:
    """Identidad visual reutilizable entre briefs de un negocio."""

    primary_font: str
    secondary_font: str
    primary_color_hex: str
    secondary_color_hex: str
    logo_asset_id: AssetId
    safe_area: SafeArea

    def __post_init__(self) -> None:
        _validate_hex_color(self.primary_color_hex, field="primary_color_hex")
        _validate_hex_color(self.secondary_color_hex, field="secondary_color_hex")
