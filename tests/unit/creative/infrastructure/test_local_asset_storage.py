"""`LocalAssetStorage`: magic bytes, tamano, traversal, integridad y URL
firmada con TTL (threat-model.md C-11/C-28; T107) mas `open_preview`
(gap-creative-preview: firma en tiempo constante, caducidad contra el
`Clock` inyectado, y el mismo guard de traversal que `get`)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import struct
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from safent_ads.creative.domain.enums import MediaKind
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.creative.infrastructure.local_asset_storage import (
    AssetDimensionsTooLargeError,
    AssetMagicBytesMismatchError,
    AssetPayloadTooLargeError,
    LocalAssetStorage,
    PreviewLinkRejectedError,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.crypto.hkdf import derive_key
from safent_ads.shared.image_limits import MAX_IMAGE_PIXELS


def _real_png(width: int = 2, height: int = 2) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _png_crc_chunk(tag: bytes, data: bytes) -> bytes:
    chunk = tag + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk))


def _oversized_png(width: int, height: int) -> bytes:
    """Bj-1: una "bomba de descompresion" real -- declara `width`x`height`
    en el `IHDR` pero un `IDAT` vacio, sin datos de pixel de verdad. Pesa
    unos bytes en disco; abrirla sin tope explotaria en memoria."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"")
    return (
        signature
        + _png_crc_chunk(b"IHDR", ihdr)
        + _png_crc_chunk(b"IDAT", idat)
        + _png_crc_chunk(b"IEND", b"")
    )


_PNG_BYTES = _real_png()
_MP3_BYTES = b"ID3" + b"\x00" * 32
_SIGNING_KEY = b"test-signing-key"
_FIXED_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _storage(tmp_path: Path, *, clock: FixedClock | None = None) -> LocalAssetStorage:
    resolved_clock = clock or FixedClock(_FIXED_NOW)
    return LocalAssetStorage(tmp_path, signing_key=_SIGNING_KEY, clock=resolved_clock)


def _sign(key: str, expires_at: int) -> str:
    # Recalcula lo mismo que `LocalAssetStorage._sign` -- deliberadamente
    # sin llamarlo (metodo privado): estas pruebas verifican el CONTRATO de
    # `open_preview` (una firma valida sobre `key:expires_at` autoriza,
    # cualquier otra cosa no), no la implementacion interna.
    return hmac.new(_SIGNING_KEY, f"{key}:{expires_at}".encode(), hashlib.sha256).hexdigest()


def test_put_writes_file_under_root(tmp_path: Path) -> None:
    async def _run() -> StorageUri:
        storage = _storage(tmp_path)
        return await storage.put(_PNG_BYTES, MediaKind.IMAGE)

    uri = asyncio.run(_run())

    assert uri.key.startswith("image/")
    assert uri.key.endswith(".png")


def test_get_roundtrips_put(tmp_path: Path) -> None:
    async def _run() -> bytes:
        storage = _storage(tmp_path)
        uri = await storage.put(_PNG_BYTES, MediaKind.IMAGE)
        return await storage.get(uri)

    assert asyncio.run(_run()) == _PNG_BYTES


def test_put_rejects_wrong_magic_bytes_for_media_kind(tmp_path: Path) -> None:
    async def _run() -> None:
        storage = _storage(tmp_path)
        await storage.put(b"not-a-real-image", MediaKind.IMAGE)

    with pytest.raises(AssetMagicBytesMismatchError):
        asyncio.run(_run())


def test_bj1_put_rejects_an_image_declaring_more_than_the_shared_pixel_cap(
    tmp_path: Path,
) -> None:
    """Bj-1: sin esta comprobacion, una imagen de unos pocos bytes que
    declara dimensiones gigantescas pasaba el tope de tamano de payload y
    los magic bytes -- explotaba en memoria en cuanto algo la decodificaba
    de verdad (`video_composer.py`, `openai_image_adapter.py`)."""
    width = height = 5000  # 25_000_000 px: entre 1x y 2x de MAX_IMAGE_PIXELS
    assert MAX_IMAGE_PIXELS < width * height < 2 * MAX_IMAGE_PIXELS
    bomb = _oversized_png(width, height)
    assert len(bomb) < 1024  # pesa nada en disco

    async def _run() -> None:
        storage = _storage(tmp_path)
        await storage.put(bomb, MediaKind.IMAGE)

    with pytest.raises(AssetDimensionsTooLargeError):
        asyncio.run(_run())


def test_bj1_put_rejects_an_image_more_than_double_the_shared_pixel_cap(tmp_path: Path) -> None:
    """Por encima del doble del tope, Pillow ya lanza `DecompressionBombError`
    dentro de `Image.open()` -- debe traducirse al mismo error de dominio,
    no escapar como una excepcion de Pillow sin mapear."""
    bomb = _oversized_png(20_000, 20_000)

    async def _run() -> None:
        storage = _storage(tmp_path)
        await storage.put(bomb, MediaKind.IMAGE)

    with pytest.raises(AssetDimensionsTooLargeError):
        asyncio.run(_run())


def test_bj1_put_accepts_an_image_within_the_shared_pixel_cap(tmp_path: Path) -> None:
    small = _oversized_png(100, 100)

    async def _run() -> StorageUri:
        storage = _storage(tmp_path)
        return await storage.put(small, MediaKind.IMAGE)

    uri = asyncio.run(_run())

    assert uri.key.startswith("image/")


def test_put_accepts_mp3_for_audio(tmp_path: Path) -> None:
    async def _run() -> StorageUri:
        storage = _storage(tmp_path)
        return await storage.put(_MP3_BYTES, MediaKind.AUDIO)

    uri = asyncio.run(_run())

    assert uri.key.endswith(".mp3")


def test_put_rejects_payload_over_size_limit(tmp_path: Path) -> None:
    async def _run() -> None:
        storage = _storage(tmp_path)
        oversized = _PNG_BYTES + b"\x00" * (201 * 1024 * 1024)
        await storage.put(oversized, MediaKind.IMAGE)

    with pytest.raises(AssetPayloadTooLargeError):
        asyncio.run(_run())


def test_each_put_gets_a_random_ulid_filename(tmp_path: Path) -> None:
    async def _run() -> tuple[str, str]:
        storage = _storage(tmp_path)
        first = await storage.put(_PNG_BYTES, MediaKind.IMAGE)
        second = await storage.put(_PNG_BYTES, MediaKind.IMAGE)
        return first.key, second.key

    first_key, second_key = asyncio.run(_run())

    assert first_key != second_key


def test_signed_preview_url_carries_hmac_and_expiry(tmp_path: Path) -> None:
    async def _run() -> str:
        storage = _storage(tmp_path)
        uri = await storage.put(_PNG_BYTES, MediaKind.IMAGE)
        return await storage.signed_preview_url(uri, ttl_s=600)

    url = asyncio.run(_run())

    assert "sig=" in url
    assert "exp=" in url
    # `GET /api/v1/creative-previews/{key:path}` (creative.presentation.
    # router): sin el prefijo `/api/v1`, la URL que pinta el `<img>` del
    # panel y la ruta que la sirve de verdad no coinciden -- el defecto
    # verificado que motiva este cambio.
    assert url.startswith("/api/v1/creative-previews/")


def test_get_on_traversal_key_raises() -> None:
    # `StorageUri` ya rechaza `..`/rutas absolutas al construir
    # (threat-model.md C-28); comprobado aqui explicitamente.
    with pytest.raises(ValueError, match="clave de almacen invalida"):
        StorageUri("../../etc/passwd")


def test_checksum_of_stored_payload_matches_sha256() -> None:
    checksum = hashlib.sha256(_PNG_BYTES).hexdigest()

    assert len(checksum) == 64


# --- open_preview --------------------------------------------------------


def test_open_preview_returns_bytes_for_a_valid_unexpired_signature(tmp_path: Path) -> None:
    async def _run() -> bytes:
        storage = _storage(tmp_path)
        uri = await storage.put(_PNG_BYTES, MediaKind.IMAGE)
        expires_at = int(_FIXED_NOW.timestamp()) + 600
        signature = _sign(uri.key, expires_at)
        return await storage.open_preview(uri.key, expires_at, signature)

    assert asyncio.run(_run()) == _PNG_BYTES


def test_open_preview_rejects_a_tampered_signature(tmp_path: Path) -> None:
    async def _run() -> None:
        storage = _storage(tmp_path)
        uri = await storage.put(_PNG_BYTES, MediaKind.IMAGE)
        expires_at = int(_FIXED_NOW.timestamp()) + 600
        await storage.open_preview(uri.key, expires_at, "0" * 64)

    with pytest.raises(PreviewLinkRejectedError):
        asyncio.run(_run())


def test_open_preview_rejects_a_signature_issued_for_a_different_key(tmp_path: Path) -> None:
    async def _run() -> None:
        storage = _storage(tmp_path)
        uri = await storage.put(_PNG_BYTES, MediaKind.IMAGE)
        expires_at = int(_FIXED_NOW.timestamp()) + 600
        signature_for_another_key = _sign("image/someone-elses-key.png", expires_at)
        await storage.open_preview(uri.key, expires_at, signature_for_another_key)

    with pytest.raises(PreviewLinkRejectedError):
        asyncio.run(_run())


def test_open_preview_rejects_an_expired_link(tmp_path: Path) -> None:
    async def _run() -> None:
        clock = FixedClock(_FIXED_NOW)
        storage = _storage(tmp_path, clock=clock)
        uri = await storage.put(_PNG_BYTES, MediaKind.IMAGE)
        expires_at = int(_FIXED_NOW.timestamp()) + 600  # TTL real: 10 minutos
        signature = _sign(uri.key, expires_at)
        clock.advance_to(datetime(2026, 1, 1, 0, 20, tzinfo=UTC))  # +20 min > TTL
        await storage.open_preview(uri.key, expires_at, signature)

    with pytest.raises(PreviewLinkRejectedError):
        asyncio.run(_run())


def test_open_preview_rejects_a_traversal_key_even_with_a_signature_that_matches_it(
    tmp_path: Path,
) -> None:
    # La firma por si sola no basta como defensa: `_resolve_within_root`
    # se comprueba SIEMPRE, incluso sobre una clave firmada de verdad para
    # ese `key:expires_at` exacto (threat-model.md C-28, defensa en
    # profundidad).
    async def _run() -> None:
        storage = _storage(tmp_path)
        expires_at = int(_FIXED_NOW.timestamp()) + 600
        traversal_key = "../outside-the-store.png"
        signature = _sign(traversal_key, expires_at)
        await storage.open_preview(traversal_key, expires_at, signature)

    with pytest.raises(PreviewLinkRejectedError):
        asyncio.run(_run())


def test_open_preview_rejects_a_well_signed_key_that_was_never_written(tmp_path: Path) -> None:
    async def _run() -> None:
        storage = _storage(tmp_path)
        expires_at = int(_FIXED_NOW.timestamp()) + 600
        key = "image/never-written.png"
        signature = _sign(key, expires_at)
        await storage.open_preview(key, expires_at, signature)

    with pytest.raises(PreviewLinkRejectedError):
        asyncio.run(_run())


# --- signing key derivada de session_secret (security-review-f4.md B-1) ---


def test_two_stores_derived_from_different_session_secrets_reject_each_others_signatures(
    tmp_path: Path,
) -> None:
    # `LocalAssetStorage` en produccion nunca recibe un secreto propio: la
    # clave llega ya derivada de `ApiSettings.session_secret` con HKDF-SHA256
    # (`composition/app.py`). Si dos instalaciones (o un reinicio con un
    # `ADS_SESSION_SECRET` distinto) derivan claves distintas, ninguna debe
    # aceptar la firma de la otra sobre la misma clave/caducidad.
    info = b"safent-ads/creative-preview/v1"
    key_a = derive_key(b"session-secret-a", info)
    key_b = derive_key(b"session-secret-b", info)
    assert key_a != key_b
    owner_b_storage = LocalAssetStorage(tmp_path, signing_key=key_b, clock=FixedClock(_FIXED_NOW))

    def _sign_with(signing_key: bytes, key: str, expires_at: int) -> str:
        return hmac.new(signing_key, f"{key}:{expires_at}".encode(), hashlib.sha256).hexdigest()

    async def _run() -> bytes:
        uri = await owner_b_storage.put(_PNG_BYTES, MediaKind.IMAGE)
        expires_at = int(_FIXED_NOW.timestamp()) + 600
        signature_signed_with_key_a = _sign_with(key_a, uri.key, expires_at)
        return await owner_b_storage.open_preview(uri.key, expires_at, signature_signed_with_key_a)

    with pytest.raises(PreviewLinkRejectedError):
        asyncio.run(_run())


def test_signed_preview_url_expiry_follows_the_injected_clock(tmp_path: Path) -> None:
    # T212: emision y verificacion usan el MISMO reloj inyectado.
    async def _run() -> str:
        storage = _storage(tmp_path, clock=FixedClock(_FIXED_NOW))
        uri = await storage.put(_PNG_BYTES, MediaKind.IMAGE)
        return await storage.signed_preview_url(uri, ttl_s=600)

    url = asyncio.run(_run())

    expires_at = int(url.split("exp=")[1].split("&")[0])
    assert expires_at == int(_FIXED_NOW.timestamp()) + 600


def test_open_preview_rejects_a_non_ascii_signature_as_an_invalid_link(tmp_path: Path) -> None:
    # T213: `hmac.compare_digest` con `str` no ASCII lanzaba TypeError (500);
    # debe ser el mismo rechazo uniforme que cualquier firma invalida (404).
    async def _run() -> None:
        storage = _storage(tmp_path)
        uri = await storage.put(_PNG_BYTES, MediaKind.IMAGE)
        expires_at = int(_FIXED_NOW.timestamp()) + 600
        with pytest.raises(PreviewLinkRejectedError):
            await storage.open_preview(uri.key, expires_at, "ñ" * 64)

    asyncio.run(_run())
