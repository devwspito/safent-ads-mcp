"""`LocalBrandAssetStorage`: magic bytes por `AssetKind`, tamano e
integridad (threat-model.md C-11/C-12, mismo criterio que
`creative.infrastructure.local_asset_storage.LocalAssetStorage`)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.infrastructure.local_brand_asset_storage import (
    BrandAssetIntegrityError,
    BrandAssetMagicBytesMismatchError,
    BrandAssetPayloadTooLargeError,
    LocalBrandAssetStorage,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_SVG_BYTES = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
_WOFF2_BYTES = b"wOF2" + b"\x00" * 32
_PALETTE_JSON = json.dumps({"swatches": [{"hex": "#112233"}]}).encode()


def _storage(tmp_path: Path) -> LocalBrandAssetStorage:
    return LocalBrandAssetStorage(tmp_path)


def test_put_writes_raster_logo_under_kind_prefix(tmp_path: Path) -> None:
    async def _run() -> str:
        return await _storage(tmp_path).put(_PNG_BYTES, AssetKind.LOGO_RASTER)

    key = asyncio.run(_run())

    assert key.startswith("logo_raster/")


def test_put_accepts_svg_for_logo_vector(tmp_path: Path) -> None:
    async def _run() -> str:
        return await _storage(tmp_path).put(_SVG_BYTES, AssetKind.LOGO_VECTOR)

    key = asyncio.run(_run())

    assert key.endswith(".svg")


def test_put_rejects_svg_bytes_for_raster_kind(tmp_path: Path) -> None:
    async def _run() -> None:
        await _storage(tmp_path).put(_SVG_BYTES, AssetKind.LOGO_RASTER)

    with pytest.raises(BrandAssetMagicBytesMismatchError):
        asyncio.run(_run())


def test_put_accepts_woff2_for_font_file(tmp_path: Path) -> None:
    async def _run() -> str:
        return await _storage(tmp_path).put(_WOFF2_BYTES, AssetKind.FONT_FILE)

    key = asyncio.run(_run())

    assert key.startswith("font_file/")


def test_put_rejects_non_font_bytes_for_font_file(tmp_path: Path) -> None:
    async def _run() -> None:
        await _storage(tmp_path).put(_PNG_BYTES, AssetKind.FONT_FILE)

    with pytest.raises(BrandAssetMagicBytesMismatchError):
        asyncio.run(_run())


def test_put_accepts_valid_json_for_palette_definition(tmp_path: Path) -> None:
    async def _run() -> str:
        return await _storage(tmp_path).put(_PALETTE_JSON, AssetKind.PALETTE_DEFINITION)

    key = asyncio.run(_run())

    assert key.endswith(".json")


def test_put_rejects_malformed_json_for_palette_definition(tmp_path: Path) -> None:
    async def _run() -> None:
        await _storage(tmp_path).put(b"not json", AssetKind.PALETTE_DEFINITION)

    with pytest.raises(BrandAssetMagicBytesMismatchError):
        asyncio.run(_run())


def test_put_rejects_payload_over_size_limit(tmp_path: Path) -> None:
    async def _run() -> None:
        oversized = _PNG_BYTES + b"\x00" * (11 * 1024 * 1024)
        await _storage(tmp_path).put(oversized, AssetKind.LOGO_RASTER)

    with pytest.raises(BrandAssetPayloadTooLargeError):
        asyncio.run(_run())


def test_get_returns_the_bytes_a_previous_put_wrote(tmp_path: Path) -> None:
    async def _run() -> tuple[bytes, bytes]:
        storage = _storage(tmp_path)
        key = await storage.put(_PNG_BYTES, AssetKind.LOGO_RASTER)
        return _PNG_BYTES, await storage.get(key)

    written, read_back = asyncio.run(_run())

    assert read_back == written


def test_get_rejects_a_key_that_escapes_the_storage_root(tmp_path: Path) -> None:
    async def _run() -> None:
        await _storage(tmp_path).get("../../etc/passwd")

    with pytest.raises(BrandAssetIntegrityError):
        asyncio.run(_run())


def test_each_put_gets_a_random_key(tmp_path: Path) -> None:
    async def _run() -> tuple[str, str]:
        storage = _storage(tmp_path)
        first = await storage.put(_PNG_BYTES, AssetKind.ICON)
        second = await storage.put(_PNG_BYTES, AssetKind.ICON)
        return first, second

    first_key, second_key = asyncio.run(_run())

    assert first_key != second_key
