"""Resuelve el `Content-Type` HTTP seguro de un activo de marca ya leido
del almacen, para `GET /api/v1/brand/assets/{asset_id}/preview`
(threat-model.md C-11/C-27, `svg_sanitizer.py` docstring: "el dia que el
panel los sirva ... se convierte en XSS" -- ese dia es este).

Nunca confia en la extension del fichero guardado (`_EXTENSION_BY_KIND`
de `local_brand_asset_storage.py` es solo un nombre de fichero, no una
garantia de contenido): el tipo raster sale de sus magic bytes, igual que
`local_brand_asset_storage._looks_like_raster` valida en la escritura.

Un SVG solo se sirve `image/svg+xml` tras pasar el mismo saneado en lista
blanca (`sanitize_remote_svg`) que `website_brand_extractor.py` ya aplica
a todo SVG rastreado de un sitio de un tercero -- pero `UploadBrandAsset.
from_bytes` (subida manual, `POST /brand/assets`) nunca lo aplico antes
de escribir en disco (solo valida magic bytes de forma), asi que este
modulo es el UNICO punto que lo garantiza antes de que el byte salga por
HTTP, sea cual sea su origen. `sanitize_remote_svg` sobre un SVG ya
saneado (ruta de rastreo) es idempotente -- no cambia nada, solo
reafirma la garantia."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.brand.infrastructure.svg_sanitizer import sanitize_remote_svg
from safent_ads.shared.errors import InfrastructureError

_RASTER_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
)
_SVG_CONTENT_TYPE = "image/svg+xml"


class UnpreviewableBrandAssetError(InfrastructureError):
    """El payload no es una imagen que este endpoint pueda servir con un
    `Content-Type` seguro (415) -- raster sin magic bytes reconocidos, o
    SVG que no supera `sanitize_remote_svg` (rechazado, no reparado)."""


@dataclass(frozen=True, kw_only=True, slots=True)
class PreviewableAsset:
    content_type: str
    body: bytes


def resolve_preview(payload: bytes) -> PreviewableAsset:
    raster_content_type = _sniff_raster(payload)
    if raster_content_type is not None:
        return PreviewableAsset(content_type=raster_content_type, body=payload)
    sanitized = sanitize_remote_svg(payload)
    if sanitized is not None:
        return PreviewableAsset(content_type=_SVG_CONTENT_TYPE, body=sanitized)
    raise UnpreviewableBrandAssetError("el activo no es una imagen que se pueda previsualizar")


def _sniff_raster(payload: bytes) -> str | None:
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    for signature, content_type in _RASTER_SIGNATURES:
        if payload.startswith(signature):
            return content_type
    return None
