"""`GET /api/v1/creative-previews/{key}` de punta a punta sobre
`create_app()` real (sesion real de `iam`, Postgres real, mismo patron que
`tests/integration/brand/test_authorization_integration.py`): la ruta
nunca toca `CreativeAssetRepository` -- opera solo sobre el almacen y la
firma -- asi que no hace falta sembrar ningun `CreativeAsset`, solo un
propietario con sesion valida y bytes ya escritos por
`LocalAssetStorage.put`.

Complementa `tests/unit/creative/infrastructure/test_local_asset_storage.py`
(firma/caducidad/traversal contra el puerto en aislamiento): este archivo
prueba la pieza que ese no puede -- la cookie de sesion real (401 sin
ella) y la respuesta HTTP completa (cuerpo, `Content-Type`,
`Cache-Control`, `ETag`/304)."""

from __future__ import annotations

import hashlib
import hmac
import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from safent_ads.composition.app import _CREATIVE_PREVIEW_SIGNING_KEY_INFO, create_app
from safent_ads.composition.settings import ApiSettings
from safent_ads.creative.domain.enums import MediaKind
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.creative.infrastructure.local_asset_storage import LocalAssetStorage
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.shared.clock import SystemClock
from safent_ads.shared.crypto.hkdf import derive_key

pytestmark = pytest.mark.integration

_RAW_TOKEN = "creative-preview-integration-test-raw-session-token"  # noqa: S105 - fixture
_SESSION_SECRET = "creative-preview-integration-test-session-secret-0123456789abcdef"  # noqa: S105 - fixture
# La clave de firma ya no es un campo de settings propio (security-review-f4.md
# B-1): se deriva de `session_secret` con HKDF-SHA256, mismo `info` que usa
# `composition/app.py` al construir el `LocalAssetStorage` real.
_SIGNING_KEY = derive_key(_SESSION_SECRET.encode(), _CREATIVE_PREVIEW_SIGNING_KEY_INFO)
def _make_png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, "PNG")
    return buffer.getvalue()


_PNG_BYTES = _make_png_bytes()


def _api_settings(database_url: str, storage_dir: Path) -> ApiSettings:
    return ApiSettings(
        database_url=database_url,
        session_secret=_SESSION_SECRET,
        totp_enc_key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        mcp_token="test-mcp-token-abc123",
        broker_socket_path="/tmp/safent-ads-test/broker.sock",
        public_base_url="https://ads.test.ts.net",
        telegram_bot_token="123456:test-bot-token",
        telegram_owner_chat_ids=[111222333],
        approval_signing_key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        creative_asset_storage_dir=storage_dir,
        seat_authority_enabled=True,
        enterprise_origin="https://enterprise.test",
        enterprise_service_secret="a" * 64,
        enterprise_org_ids=frozenset({uuid.UUID("00000000-0000-0000-0000-000000000001")}),
    )


class _SeededOwner:
    def __init__(self, owner_id: uuid.UUID) -> None:
        self.owner_id = owner_id


@pytest.fixture
async def seeded_owner(database_url: str) -> AsyncIterator[_SeededOwner]:
    """Escribe de verdad (commit real, no savepoint): `Container` abre su
    propio motor independiente del de este fixture, asi que la fila tiene
    que estar confirmada en la base para que la vea (mismo motivo que
    `tests/integration/brand/test_authorization_integration.py`)."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO owners (id, email, password_hash) "
                "VALUES (:id, :email, :password_hash)"
            ),
            {
                "id": str(owner_id),
                "email": f"owner-{owner_id.hex[:8]}@safent.example",
                "password_hash": "argon2id$fixture$not-a-real-hash",
            },
        )
        await connection.execute(
            text(
                "INSERT INTO sessions (id, owner_id, token_hash, created_at, expires_at) "
                "VALUES (:id, :owner_id, :token_hash, :created_at, :expires_at)"
            ),
            {
                "id": str(session_id),
                "owner_id": str(owner_id),
                "token_hash": hashlib.sha256(_RAW_TOKEN.encode("utf-8")).hexdigest(),
                "created_at": now,
                "expires_at": now + timedelta(hours=1),
            },
        )
    try:
        yield _SeededOwner(owner_id)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE id = :id"), {"id": str(session_id)}
            )
            await connection.execute(
                text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)}
            )
        await engine.dispose()


async def _store_one_png(storage_dir: Path) -> StorageUri:
    store = LocalAssetStorage(storage_dir, signing_key=_SIGNING_KEY, clock=SystemClock())
    return await store.put(_PNG_BYTES, MediaKind.IMAGE)


def _sign(key: str, expires_at: int) -> str:
    return hmac.new(_SIGNING_KEY, f"{key}:{expires_at}".encode(), hashlib.sha256).hexdigest()


async def test_returns_200_with_bytes_and_security_headers_for_a_valid_link(
    seeded_owner: _SeededOwner, database_url: str, tmp_path: Path
) -> None:
    storage_dir = tmp_path / "creative-assets"
    app = create_app(_api_settings(database_url, storage_dir))
    try:
        uri = await _store_one_png(storage_dir)
        expires_at = int(datetime.now(UTC).timestamp()) + 600
        signature = _sign(uri.key, expires_at)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
        ) as client:
            response = await client.get(
                f"/api/v1/creative-previews/{uri.key}",
                params={"exp": expires_at, "sig": signature},
            )
        assert response.status_code == 200
        assert response.content == _PNG_BYTES
        assert response.headers["content-type"] == "image/png"
        assert response.headers["content-disposition"] == "inline"
        assert response.headers["cache-control"].startswith("private, max-age=")
        assert "etag" in response.headers
        assert response.headers["x-content-type-options"] == "nosniff"
    finally:
        await app.state.container.aclose()


async def test_returns_304_when_if_none_match_repeats_the_etag(
    seeded_owner: _SeededOwner, database_url: str, tmp_path: Path
) -> None:
    storage_dir = tmp_path / "creative-assets"
    app = create_app(_api_settings(database_url, storage_dir))
    try:
        uri = await _store_one_png(storage_dir)
        expires_at = int(datetime.now(UTC).timestamp()) + 600
        signature = _sign(uri.key, expires_at)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
        ) as client:
            path_and_query = f"/api/v1/creative-previews/{uri.key}"
            params = {"exp": expires_at, "sig": signature}
            first = await client.get(path_and_query, params=params)
            second = await client.get(
                path_and_query, params=params, headers={"if-none-match": first.headers["etag"]}
            )
        assert second.status_code == 304
        assert second.content == b""
    finally:
        await app.state.container.aclose()


async def test_returns_404_for_an_expired_link(
    seeded_owner: _SeededOwner, database_url: str, tmp_path: Path
) -> None:
    storage_dir = tmp_path / "creative-assets"
    app = create_app(_api_settings(database_url, storage_dir))
    try:
        uri = await _store_one_png(storage_dir)
        expired_at = int(datetime.now(UTC).timestamp()) - 60
        signature = _sign(uri.key, expired_at)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
        ) as client:
            response = await client.get(
                f"/api/v1/creative-previews/{uri.key}",
                params={"exp": expired_at, "sig": signature},
            )
        assert response.status_code == 404
    finally:
        await app.state.container.aclose()


async def test_returns_404_for_a_tampered_signature(
    seeded_owner: _SeededOwner, database_url: str, tmp_path: Path
) -> None:
    storage_dir = tmp_path / "creative-assets"
    app = create_app(_api_settings(database_url, storage_dir))
    try:
        uri = await _store_one_png(storage_dir)
        expires_at = int(datetime.now(UTC).timestamp()) + 600
        valid_signature = _sign(uri.key, expires_at)
        tampered_signature = ("0" if valid_signature[0] != "0" else "1") + valid_signature[1:]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
        ) as client:
            response = await client.get(
                f"/api/v1/creative-previews/{uri.key}",
                params={"exp": expires_at, "sig": tampered_signature},
            )
        assert response.status_code == 404
    finally:
        await app.state.container.aclose()


async def test_returns_401_without_a_session_cookie(database_url: str, tmp_path: Path) -> None:
    storage_dir = tmp_path / "creative-assets"
    app = create_app(_api_settings(database_url, storage_dir))
    try:
        uri = await _store_one_png(storage_dir)
        expires_at = int(datetime.now(UTC).timestamp()) + 600
        signature = _sign(uri.key, expires_at)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/creative-previews/{uri.key}",
                params={"exp": expires_at, "sig": signature},
            )
        assert response.status_code == 401
    finally:
        await app.state.container.aclose()
