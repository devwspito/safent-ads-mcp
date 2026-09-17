"""`AssetSpec`: peticion de una pieza dentro de la matriz copy x visual que
arma `GenerateCreativeAssets`, previa a convertirse en `ImageSpec` /
`VideoSpec` / `BannerSpec` concretos (creative-port.md §"Seleccion de
renderizador"). No esta en `creative-port.md` como dataclase — es el nivel
de abstraccion que pide el encargo del carril, mas alto que los tres specs
por renderizador."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.creative.domain.brand_kit import SafeArea
from safent_ads.creative.domain.enums import AssetKind, Format, VideoDurationSeconds


class AssetSpecError(ValueError):
    """Violacion de un invariante de `AssetSpec`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AssetSpec:
    kind: AssetKind
    format: Format
    safe_area: SafeArea
    duration: VideoDurationSeconds | None = None

    def __post_init__(self) -> None:
        if self.kind == AssetKind.VIDEO and self.duration is None:
            raise AssetSpecError("un AssetSpec de video requiere duration")
        if self.kind != AssetKind.VIDEO and self.duration is not None:
            raise AssetSpecError(f"duration no aplica a kind={self.kind!r}")
