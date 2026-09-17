"""`LocalAssetStorage` implementa `AssetStorePort` sobre disco local
(threat-model.md C-28): tipo por magic bytes, nombre aleatorio (ULID),
almacen fuera del webroot, integridad sha256 verificada tras escribir, y
`signed_preview_url` con TTL para que el panel nunca reciba una URL libre
(threat-model.md C-11)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from ulid import ULID

from safent_ads.creative.domain.enums import MediaKind
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError
from safent_ads.shared.image_limits import MAX_IMAGE_PIXELS

_EXTENSION_BY_MEDIA_KIND: dict[MediaKind, str] = {
    MediaKind.IMAGE: ".png",
    MediaKind.VIDEO: ".mp4",
    MediaKind.BANNER: ".png",
    MediaKind.AUDIO: ".mp3",
}

# Magic bytes suficientes para distinguir los cuatro contenedores que este
# almacen acepta; no es un validador de codec completo, es la comprobacion
# minima de threat-model.md C-28 ("tipo por magic bytes").
_MAGIC_BYTES_BY_MEDIA_KIND: dict[MediaKind, tuple[bytes, ...]] = {
    MediaKind.IMAGE: (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"),
    MediaKind.BANNER: (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"),
    MediaKind.VIDEO: (b"\x00\x00\x00", b"ftyp"),
    MediaKind.AUDIO: (b"ID3", b"\xff\xfb", b"\xff\xf3", b"RIFF"),
}

_MAX_PAYLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB: acota vídeos generados

# Bj-1: una imagen "bomba de descompresion" puede pesar poco en disco (unos
# KiB) y declarar unas dimensiones astronomicas -- pequena de sobra para
# `_MAX_PAYLOAD_BYTES`, pero explota en memoria en cuanto algo la decodifica
# (`video_composer.py`, `openai_image_adapter.py`). Solo aplica a los
# `MediaKind` que Pillow sabe abrir; `VIDEO`/`AUDIO` no pasan por aqui.
_IMAGE_LIKE_MEDIA_KINDS = frozenset({MediaKind.IMAGE, MediaKind.BANNER})


class AssetPayloadTooLargeError(InfrastructureError):
    """El payload supera el tamano maximo aceptado por el almacen."""


class AssetDimensionsTooLargeError(InfrastructureError):
    """Una imagen supera el tope compartido de pixeles
    (`shared/image_limits.MAX_IMAGE_PIXELS`) -- bomba de descompresion."""


class AssetMagicBytesMismatchError(InfrastructureError):
    """Los primeros bytes del payload no encajan con `media_kind`."""


class AssetIntegrityError(InfrastructureError):
    """El fichero releido de disco no coincide con el sha256 del payload."""


class PreviewLinkRejectedError(InfrastructureError):
    """Un enlace de `signed_preview_url` no supera la verificacion en
    `open_preview`: firma invalida, caducado, o la clave resuelve fuera
    del almacen. Deliberadamente UN solo tipo para los tres motivos --
    `creative.presentation.router` responde siempre el mismo 404, nunca
    revela cual de los tres fallo (mismo criterio IDOR que el resto del
    router, threat-model.md C-27)."""


def _looks_like(payload: bytes, media_kind: MediaKind) -> bool:
    if media_kind == MediaKind.VIDEO:
        # ISO-BMFF (mp4/mov): "ftyp" box suele empezar en el byte 4.
        return payload[4:8] == b"ftyp"
    signatures = _MAGIC_BYTES_BY_MEDIA_KIND[media_kind]
    return any(payload.startswith(sig) for sig in signatures)


def _new_filename(media_kind: MediaKind) -> str:
    return f"{ULID()}{_EXTENSION_BY_MEDIA_KIND[media_kind]}"


class LocalAssetStorage:
    def __init__(self, root_dir: Path, signing_key: bytes, clock: Clock) -> None:
        self._root_dir = root_dir.resolve()
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._signing_key = signing_key
        self._clock = clock

    async def put(self, payload: bytes, media_kind: MediaKind) -> StorageUri:
        self._require_size_within_limit(payload)
        self._require_magic_bytes_match(payload, media_kind)
        await self._require_dimensions_within_limit(payload, media_kind)
        key = f"{media_kind.value}/{_new_filename(media_kind)}"
        target = self._resolve_within_root(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, payload)
        await self._verify_written_integrity(target, payload)
        return StorageUri(key)

    async def get(self, uri: StorageUri) -> bytes:
        """`AssetRetrievalPort`: lectura defensiva, misma validacion de
        traversal que `put` aunque `StorageUri` ya se valida al construir."""
        target = self._resolve_within_root(uri.key)
        return await asyncio.to_thread(target.read_bytes)

    async def signed_preview_url(self, uri: StorageUri, ttl_s: int) -> str:
        # Mismo reloj que `open_preview`: emitir y verificar con dos relojes
        # distintos (T212) hacia que el TTL real dependiera del desfase.
        expires_at = int(self._clock.now().timestamp()) + ttl_s
        signature = self._sign(uri.key, expires_at)
        # Prefijo `/api/v1` a proposito: el panel pinta este valor
        # directamente en un `<img src=...>` (mismo patron que
        # `brand.presentation.serializers.asset_preview_url`), nunca a
        # traves de `apiClient` -- sin el prefijo, la ruta de
        # `creative.presentation.router` y esta URL no coinciden y todo
        # `<img>` del panel recibe un 404.
        return f"/api/v1/creative-previews/{uri.key}?exp={expires_at}&sig={signature}"

    async def open_preview(self, key: str, expires_at: int, signature: str) -> bytes:
        """Verifica firma + caducidad y devuelve los bytes ya leidos de
        disco. Reusa `_sign` (misma clave/mensaje que emitio la URL) y
        `_resolve_within_root` (mismo guard de traversal que `get`) -- ver
        `AssetStorePort.open_preview` para el contrato de un unico tipo de
        error."""
        expected_signature = self._sign(key, expires_at)
        # En bytes: `compare_digest` con `str` no ASCII lanza TypeError y el
        # cliente veria un 500 en vez del 404 uniforme (T213).
        if not hmac.compare_digest(
            expected_signature.encode("ascii"), signature.encode("utf-8", "replace")
        ):
            raise PreviewLinkRejectedError("firma invalida")
        if expires_at < int(self._clock.now().timestamp()):
            raise PreviewLinkRejectedError("enlace caducado")
        try:
            target = self._resolve_within_root(key)
        except AssetIntegrityError as exc:
            raise PreviewLinkRejectedError("clave fuera del almacen") from exc
        try:
            return await asyncio.to_thread(target.read_bytes)
        except OSError as exc:
            # `OSError` (no solo `FileNotFoundError`): una clave vacia
            # resuelve al propio `root_dir` -- pasa `_resolve_within_root`
            # porque coincide, no porque sea segura -- y leerlo como
            # fichero lanza `IsADirectoryError`, otra subclase de `OSError`.
            raise PreviewLinkRejectedError("activo no encontrado") from exc

    def _require_size_within_limit(self, payload: bytes) -> None:
        if len(payload) > _MAX_PAYLOAD_BYTES:
            raise AssetPayloadTooLargeError(f"{len(payload)} bytes > {_MAX_PAYLOAD_BYTES}")

    def _require_magic_bytes_match(self, payload: bytes, media_kind: MediaKind) -> None:
        if not _looks_like(payload, media_kind):
            raise AssetMagicBytesMismatchError(f"payload no parece {media_kind.value}")

    async def _require_dimensions_within_limit(self, payload: bytes, media_kind: MediaKind) -> None:
        if media_kind not in _IMAGE_LIKE_MEDIA_KINDS:
            return
        await asyncio.to_thread(self._check_image_dimensions, payload, media_kind)

    def _check_image_dimensions(self, payload: bytes, media_kind: MediaKind) -> None:
        try:
            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.width, image.height
        except Image.DecompressionBombError as exc:
            # Por encima del doble del tope, Pillow ya lo rechaza el
            # mismo dentro de `Image.open()` -- mismo motivo que nuestra
            # comprobacion explicita de abajo, mismo error de dominio.
            raise AssetDimensionsTooLargeError(str(exc)) from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise AssetMagicBytesMismatchError(f"payload no parece {media_kind.value}") from exc
        if width * height > MAX_IMAGE_PIXELS:
            # Entre 1x y 2x del tope, Pillow solo emite un
            # `DecompressionBombWarning` (no interrumpe) -- de ahi que la
            # comprobacion explicita sea necesaria, no baste con dejarselo
            # a Pillow.
            raise AssetDimensionsTooLargeError(
                f"{width}x{height} ({width * height} px) > {MAX_IMAGE_PIXELS} px"
            )

    def _resolve_within_root(self, key: str) -> Path:
        candidate = (self._root_dir / key).resolve()
        if candidate != self._root_dir and self._root_dir not in candidate.parents:
            raise AssetIntegrityError(f"clave fuera del almacen: {key!r}")
        return candidate

    async def _verify_written_integrity(self, target: Path, payload: bytes) -> None:
        written = await asyncio.to_thread(target.read_bytes)
        if hashlib.sha256(written).digest() != hashlib.sha256(payload).digest():
            raise AssetIntegrityError(f"el fichero escrito no coincide con el payload: {target}")

    def _sign(self, key: str, expires_at: int) -> str:
        message = f"{key}:{expires_at}".encode()
        return hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()
