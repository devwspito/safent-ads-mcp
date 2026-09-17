"""Resuelve el `Content-Type` HTTP seguro de un activo de `creative` ya
leido del almacen, para `GET /api/v1/creative-previews/{key}`
(`creative.presentation.router`). Mismo principio que
`brand.infrastructure.asset_preview_content_type`: nunca confiar en la
extension del fichero guardado (`_EXTENSION_BY_MEDIA_KIND` de
`local_asset_storage.py` es solo un nombre de fichero) -- el tipo sale de
sus magic bytes.

No es el mismo modulo que el de `brand` a proposito: `creative` nunca
almacena SVG (no esta en `_EXTENSION_BY_MEDIA_KIND`), asi que no necesita
`sanitize_remote_svg`; en cambio si almacena video (`ftyp`) y audio
(`ID3`/frame MP3/`RIFF`+`WAVE`, `chatterbox_tts_adapter.py` produce WAV),
que `brand` nunca sirve. Mover esto a `shared/` acoplaria dos resolutores
con conjuntos de formatos disjuntos sin ganar nada -- cada contexto
mantiene el suyo, igual que ya hace `brand`."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.shared.errors import InfrastructureError

_RASTER_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)
_MP3_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"ID3", "audio/mpeg"),
    (b"\xff\xfb", "audio/mpeg"),
    (b"\xff\xf3", "audio/mpeg"),
)
_VIDEO_CONTENT_TYPE = "video/mp4"
_WAV_CONTENT_TYPE = "audio/wav"


class UnpreviewableCreativeAssetError(InfrastructureError):
    """El payload no encaja con ningun tipo que este endpoint sepa servir
    con un `Content-Type` seguro (415) -- no deberia ocurrir sobre un
    activo que ya paso `LocalAssetStorage.put`, pero la ruta nunca confia
    en esa garantia por si sola."""


@dataclass(frozen=True, kw_only=True, slots=True)
class PreviewableCreativeAsset:
    content_type: str
    body: bytes


def resolve_creative_preview_content_type(payload: bytes) -> PreviewableCreativeAsset:
    content_type = _sniff(payload)
    if content_type is None:
        raise UnpreviewableCreativeAssetError("el activo no es previsualizable")
    return PreviewableCreativeAsset(content_type=content_type, body=payload)


def _sniff(payload: bytes) -> str | None:
    if payload[4:8] == b"ftyp":
        return _VIDEO_CONTENT_TYPE
    if payload[:4] == b"RIFF" and payload[8:12] == b"WAVE":
        return _WAV_CONTENT_TYPE
    for signature, content_type in (*_RASTER_SIGNATURES, *_MP3_SIGNATURES):
        if payload.startswith(signature):
            return content_type
    return None
