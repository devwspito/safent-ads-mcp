"""`LocalBrandAssetStorage` implementa `BrandAssetStoragePort` sobre disco
local: tipo por magic bytes, nombre aleatorio (ULID), integridad sha256
verificada tras escribir -- mismo patron que
`creative.infrastructure.local_asset_storage.LocalAssetStorage`, repetido
aqui a proposito porque `brand` no puede importar `creative` sin invertir
el grafo de dependencias (plan.md §4; `application/ports.py` documenta el
mismo principio).

Tope de tamano mucho mas bajo que el de `creative` (200 MiB, pensado para
video generado): aqui los activos son logos/iconos/fuentes/JSON de un
sitio de un tercero no confiable, nunca deberian pesar mas que unos pocos
MiB (threat-model.md C-11/C-12, defensa en profundidad junto al streaming
cap de `website_brand_extractor.py`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from ulid import ULID

from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.shared.errors import InfrastructureError

MAX_BRAND_ASSET_BYTES = 10 * 1024 * 1024  # 10 MiB, exportado: el router
# multipart (`presentation/router.py`) corta la lectura en streaming a este
# mismo tope ANTES de construir el `bytes` que este puerto recibe -- sin
# eso, un `UploadFile` enorme se bufferizaria entero en memoria antes de
# que `_require_size_within_limit` tuviera ocasion de rechazarlo.

_EXTENSION_BY_KIND: dict[AssetKind, str] = {
    AssetKind.LOGO_VECTOR: ".svg",
    AssetKind.LOGO_RASTER: ".bin",
    AssetKind.REFERENCE_PHOTO: ".bin",
    AssetKind.ICON: ".bin",
    AssetKind.FONT_FILE: ".bin",
    AssetKind.PALETTE_DEFINITION: ".json",
}

_RASTER_SIGNATURES: tuple[bytes, ...] = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"\x00\x00\x01\x00",  # ICO
)
_FONT_SIGNATURES: tuple[bytes, ...] = (b"wOF2", b"wOFF", b"OTTO", b"\x00\x01\x00\x00")


class BrandAssetPayloadTooLargeError(InfrastructureError):
    """El payload supera `MAX_BRAND_ASSET_BYTES`."""


class BrandAssetMagicBytesMismatchError(InfrastructureError):
    """Los primeros bytes del payload no encajan con `kind`."""


class BrandAssetIntegrityError(InfrastructureError):
    """El fichero releido de disco no coincide con el sha256 del payload."""


def _looks_like_svg(payload: bytes) -> bool:
    head = payload.lstrip(b"\xef\xbb\xbf \t\r\n")[:256].lower()
    return head.startswith(b"<?xml") or head.startswith(b"<svg")


def _looks_like_raster(payload: bytes) -> bool:
    if payload[8:12] == b"WEBP" and payload[:4] == b"RIFF":
        return True
    return any(payload.startswith(sig) for sig in _RASTER_SIGNATURES)


def _looks_like_font(payload: bytes) -> bool:
    return any(payload.startswith(sig) for sig in _FONT_SIGNATURES)


def _looks_like_palette_json(payload: bytes) -> bool:
    try:
        data = json.loads(payload)
    except ValueError:
        return False
    return isinstance(data, dict | list)


_VALIDATOR_BY_KIND = {
    AssetKind.LOGO_VECTOR: _looks_like_svg,
    AssetKind.LOGO_RASTER: _looks_like_raster,
    AssetKind.REFERENCE_PHOTO: _looks_like_raster,
    AssetKind.ICON: _looks_like_raster,
    AssetKind.FONT_FILE: _looks_like_font,
    AssetKind.PALETTE_DEFINITION: _looks_like_palette_json,
}


def _new_filename(kind: AssetKind) -> str:
    return f"{ULID()}{_EXTENSION_BY_KIND[kind]}"


class LocalBrandAssetStorage:
    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir.resolve()
        self._root_dir.mkdir(parents=True, exist_ok=True)

    async def put(self, payload: bytes, kind: AssetKind) -> str:
        self._require_size_within_limit(payload)
        self._require_magic_bytes_match(payload, kind)
        key = f"{kind.value}/{_new_filename(kind)}"
        target = self._resolve_within_root(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, payload)
        await self._verify_written_integrity(target, payload)
        return key

    async def get(self, key: str) -> bytes:
        """Lectura defensiva, misma validacion de traversal que `put`
        (mismo patron que `creative.infrastructure.local_asset_storage.
        LocalAssetStorage.get`): `key` siempre viene resuelto por un
        repositorio de `application` (`GetBrandAssetPreview`), nunca de la
        peticion HTTP directamente."""
        target = self._resolve_within_root(key)
        return await asyncio.to_thread(target.read_bytes)

    def _require_size_within_limit(self, payload: bytes) -> None:
        if len(payload) > MAX_BRAND_ASSET_BYTES:
            raise BrandAssetPayloadTooLargeError(f"{len(payload)} bytes > {MAX_BRAND_ASSET_BYTES}")

    def _require_magic_bytes_match(self, payload: bytes, kind: AssetKind) -> None:
        if not _VALIDATOR_BY_KIND[kind](payload):
            raise BrandAssetMagicBytesMismatchError(f"payload no parece {kind.value}")

    def _resolve_within_root(self, key: str) -> Path:
        candidate = (self._root_dir / key).resolve()
        if candidate != self._root_dir and self._root_dir not in candidate.parents:
            raise BrandAssetIntegrityError(f"clave fuera del almacen: {key!r}")
        return candidate

    async def _verify_written_integrity(self, target: Path, payload: bytes) -> None:
        written = await asyncio.to_thread(target.read_bytes)
        if hashlib.sha256(written).digest() != hashlib.sha256(payload).digest():
            raise BrandAssetIntegrityError(
                f"el fichero escrito no coincide con el payload: {target}"
            )
