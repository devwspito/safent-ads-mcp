"""Recorte generico de un visual generado al `Format` de anuncio exacto que
se pidio (correccion del propietario 2026-09-09: "keep the crop/scale math
generic, driven by whatever the backend returns" — nunca una tabla fija de
tamanos de un proveedor concreto). Geometria pura: sin bytes, sin Pillow;
opera sobre las dimensiones enteras reales del activo que vuelva, sean
cuales sean. La infraestructura (`openai_image_adapter.py`) mide el activo
recibido y aplica el `CropBox` resultante con Pillow.

Solo recorta el eje que sobra respecto al `Format` pedido — nunca amplia
(ampliar antes de recortar pierde nitidez, y el reescalado final a la
resolucion exacta del anuncio ya sucede aparte). Con la relacion de
aspecto exigida a un `ImageSpec`/`VideoSpec` (1:3-3:1,
`Format.is_renderer_eligible`), el eje recortado nunca necesita mas
pixeles de los que ya trae la fuente; el `min()` en `_crop_*` solo absorbe
el redondeo de `round()` en el limite exacto, no representa un caso real
de "no cabe"."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.creative.domain.brand_kit import SafeArea
from safent_ads.creative.domain.enums import Format


class InvalidSourceDimensionsError(ValueError):
    """`source_width`/`source_height` no son enteros positivos."""


@dataclass(frozen=True, slots=True)
class CropBox:
    """Rectangulo, en pixeles del activo fuente, a recortar antes de
    reescalar al `Format` exacto pedido."""

    left: int
    top: int
    width: int
    height: int


def crop_to_format(
    source_width: int,
    source_height: int,
    target: Format,
    *,
    safe_area: SafeArea | None = None,
) -> CropBox:
    """`CropBox` centrado (o desplazado por `safe_area`) que hace que el
    activo fuente, tras recortarlo y reescalarlo, tenga exactamente la
    relacion de aspecto de `target`."""
    if source_width <= 0 or source_height <= 0:
        raise InvalidSourceDimensionsError(f"{source_width}x{source_height}")
    target_ratio = target.width / target.height
    source_ratio = source_width / source_height
    if source_ratio > target_ratio:
        return _crop_width(source_width, source_height, target_ratio, safe_area)
    return _crop_height(source_width, source_height, target_ratio, safe_area)


def _crop_width(
    source_width: int, source_height: int, target_ratio: float, safe_area: SafeArea | None
) -> CropBox:
    cropped_width = min(round(source_height * target_ratio), source_width)
    left = _anchored_offset(source_width, cropped_width, safe_area, axis="x")
    return CropBox(left=left, top=0, width=cropped_width, height=source_height)


def _crop_height(
    source_width: int, source_height: int, target_ratio: float, safe_area: SafeArea | None
) -> CropBox:
    cropped_height = min(round(source_width / target_ratio), source_height)
    top = _anchored_offset(source_height, cropped_height, safe_area, axis="y")
    return CropBox(left=0, top=top, width=source_width, height=cropped_height)


def _anchored_offset(
    source_len: int, cropped_len: int, safe_area: SafeArea | None, *, axis: str
) -> int:
    """Centrado salvo que `safe_area` reserve mas margen en un extremo que
    en el opuesto: el recorte se desplaza hacia el extremo con MENOS
    margen reservado, para no comerse el lado que la marca ya declaro
    protegido (donde suele vivir el sujeto)."""
    slack = source_len - cropped_len
    if safe_area is None or slack == 0:
        return slack // 2
    low_margin, high_margin = (
        (safe_area.left, safe_area.right) if axis == "x" else (safe_area.top, safe_area.bottom)
    )
    if low_margin == high_margin:
        return slack // 2
    return slack if low_margin > high_margin else 0
