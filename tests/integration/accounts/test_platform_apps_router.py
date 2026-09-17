"""`/api/v1/platform-apps*` de punta a punta (owner decision,
app-credentials-ui, contracts/rest-api.md §Conexiones): Postgres real (sesion +
TOTP, `require_reauth`) y un bróker real sobre un socket Unix real (mismo
patrón que `test_connections_router.py`) -- solo el HTTP saliente a
Google/Meta está mockeado, y nunca se llega a invocar porque `PUT` no
completa ningún OAuth.

`PUT → status → reconnect/start ya no 409` prueba que `platform_apps_router`
y `connections_router` comparten el MISMO almacén cifrado del bróker: el
propietario teclea la app y puede conectar una cuenta sin reiniciar nada."""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pyotp
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.accounts.presentation.connections_router import build_connections_router
from safent_ads.accounts.presentation.platform_apps_router import build_platform_apps_router
from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.platforms.dynamic_oauth_adapters import (
    DynamicGoogleOAuthAdapter,
    DynamicMetaOAuthAdapter,
)
from safent_ads.broker.presentation.dispatcher import BrokerRuntime
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_TOTP_SECRET,
    AesGcmTotpCipher,
)
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.shared.clock import SystemClock
from tests.integration.iam.confirmation_helpers import confirmed_request
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_KEY_B64 = base64.b64encode(b"0" * 32).decode()
_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_TOTP_SECRET = pyotp.random_base32()

_VALID_GOOGLE_BODY = {
    "client_id": "abc123.apps.googleusercontent.com",
    "client_secret": "super-secret-client-secret",
    "login_customer_id": "1234567890",
}


class _UnreachableHttpClient:
    async def post_form(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        raise AssertionError("no deberia llamar a la red en estos tests")

    async def get_json(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        raise AssertionError("no deberia llamar a la red en estos tests")

    async def post_json(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        raise AssertionError("no deberia llamar a la red en estos tests")


class _SeededOwner:
    def __init__(self, *, owner_id: uuid.UUID, raw_token: str) -> None:
        self.owner_id = owner_id
        self.raw_token = raw_token


async def _seed_owner(session: AsyncSession, *, with_totp: bool) -> _SeededOwner:
    owner_id = uuid.uuid4()
    raw_token = f"platform-apps-test-token-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    encrypted_secret = (
        AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64).encrypt(_TOTP_SECRET, purpose=PURPOSE_TOTP_SECRET)
        if with_totp
        else None
    )
    await session.execute(
        text(
            "INSERT INTO owners (id, email, password_hash, totp_secret_encrypted, "
            "totp_confirmed_at) VALUES (:id, :email, :password_hash, :totp_secret, :now)"
        ),
        {
            "id": str(owner_id),
            "email": f"owner-{owner_id.hex[:8]}@safent.example",
            "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
            "totp_secret": encrypted_secret,
            "now": now if with_totp else None,
        },
    )
    await session.execute(
        text(
            "INSERT INTO sessions (id, owner_id, token_hash, created_at, expires_at) "
            "VALUES (:id, :owner_id, :token_hash, :created_at, :expires_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "owner_id": str(owner_id),
            "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            "created_at": now,
            "expires_at": now + timedelta(hours=1),
        },
    )
    return _SeededOwner(owner_id=owner_id, raw_token=raw_token)


async def _seed_business(session: AsyncSession) -> uuid.UUID:
    business_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
            "VALUES (:id, :slug, 'Negocio de contrato', 'Europe/Madrid', 'EUR')"
        ),
        {"id": str(business_id), "slug": f"neg-{business_id.hex[:12]}"},
    )
    return business_id


@pytest.fixture
async def owner(database_url: str) -> AsyncIterator[_SeededOwner]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        seeded = await _seed_owner(session, with_totp=True)
        await session.commit()
    try:
        yield seeded
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(seeded.owner_id)}
            )
        await engine.dispose()


@pytest.fixture
async def business_id(database_url: str) -> uuid.UUID:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        seeded = await _seed_business(session)
        await session.commit()
    await engine.dispose()
    return seeded


def _broker_runtime(store_dir: Path) -> BrokerRuntime:
    clock = SystemClock()
    http = _UnreachableHttpClient()
    store = EncryptedCredentialStore(store_dir, _KEY_B64)
    google = DynamicGoogleOAuthAdapter(store, http, clock)  # type: ignore[arg-type]
    meta = DynamicMetaOAuthAdapter(store, http, clock)  # type: ignore[arg-type]
    return BrokerRuntime(
        adapters=PlatformAdapterRegistry({}),
        oauth_flow=OAuthConnectFlow(store, google, meta, clock),
        app_credentials=AppCredentialsService(store, clock),
    )


@pytest.fixture
async def app_and_broker(database_url: str, tmp_path: Path) -> AsyncIterator[FastAPI]:
    socket_path = tmp_path / "broker.sock"
    server = await serve(
        socket_path, _broker_runtime(tmp_path / "credentials"), frozenset({os.getuid()})
    )
    settings = build_api_settings(database_url=database_url, broker_socket_path=socket_path)
    container = Container.build(settings)

    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_platform_apps_router(settings))
    app.include_router(build_connections_router(settings))

    try:
        yield app
    finally:
        server.close()
        await server.wait_closed()
        await container.aclose()


def _client(app: FastAPI, *, raw_token: str | None) -> httpx.AsyncClient:
    cookies = {SESSION_COOKIE_NAME: raw_token} if raw_token else {}
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={**cookies, "ads_csrf": "test-csrf"},
        headers={"X-CSRF-Token": "test-csrf"},
    )


async def test_get_platform_apps_starts_unconfigured_with_redirect_uris(
    app_and_broker: FastAPI, owner: _SeededOwner
) -> None:
    async with _client(app_and_broker, raw_token=owner.raw_token) as client:
        response = await client.get("/api/v1/platform-apps")

    assert response.status_code == 200, response.text
    body = response.json()
    by_platform = {item["platform"]: item for item in body["items"]}
    assert by_platform["google"]["configured"] is False
    assert by_platform["google"]["redirect_uri"].endswith(
        "/api/v1/platform-accounts/google/reconnect/callback"
    )
    assert by_platform["meta"]["configured"] is False


async def test_desktop_credentials_http_confirmation_to_real_broker(
    app_and_broker: FastAPI, owner: _SeededOwner
) -> None:
    async with _client(app_and_broker, raw_token=owner.raw_token) as client:
        payload = {"client_id": "desktop.apps.googleusercontent.com", "client_type": "desktop"}
        unconfirmed = await client.put("/api/v1/platform-apps/google", json=payload)
        assert unconfirmed.status_code != 200
        response = await confirmed_request(
            client, "PUT", "/api/v1/platform-apps/google", json=payload
        )
        assert response.status_code == 200, response.text
        assert response.json()["client_type"] == "desktop"
        assert response.json()["configured"] is True
        assert "client_secret" not in response.json()
        statuses = (await client.get("/api/v1/platform-apps")).json()["items"]
        google = next(item for item in statuses if item["platform"] == "google")
        assert google["client_type"] == "desktop"


async def test_put_then_status_unblocks_reconnect_start(
    app_and_broker: FastAPI, owner: _SeededOwner, business_id: uuid.UUID
) -> None:
    async with _client(app_and_broker, raw_token=owner.raw_token) as client:
        before = await client.post(
            "/api/v1/platform-accounts/google/reconnect/start",
            params={"business_id": str(business_id)},
        )
        assert before.status_code == 409, before.text
        assert before.json()["error"]["code"] == "PLATFORM_APP_NOT_CONFIGURED"

        put_response = await confirmed_request(
            client, "PUT", "/api/v1/platform-apps/google", json=_VALID_GOOGLE_BODY
        )
        assert put_response.status_code == 200, put_response.text
        put_body = put_response.json()
        assert put_body["configured"] is True
        assert put_body["client_id_masked"] == "****.com"

        status_response = await client.get("/api/v1/platform-apps")
        status_body = status_response.json()
        google_status = next(item for item in status_body["items"] if item["platform"] == "google")
        assert google_status["configured"] is True

        after = await client.post(
            "/api/v1/platform-accounts/google/reconnect/start",
            params={"business_id": str(business_id)},
        )
        assert after.status_code == 201, after.text


async def test_secrets_never_appear_in_any_response(
    app_and_broker: FastAPI, owner: _SeededOwner
) -> None:
    async with _client(app_and_broker, raw_token=owner.raw_token) as client:
        put_response = await confirmed_request(
            client, "PUT", "/api/v1/platform-apps/google", json=_VALID_GOOGLE_BODY
        )
        status_response = await client.get("/api/v1/platform-apps")

    for response in (put_response, status_response):
        assert _VALID_GOOGLE_BODY["client_secret"] not in response.text


async def test_put_without_session_is_401(app_and_broker: FastAPI) -> None:
    async with _client(app_and_broker, raw_token=None) as client:
        response = await confirmed_request(
            client, "PUT", "/api/v1/platform-apps/google", json=_VALID_GOOGLE_BODY
        )

    assert response.status_code == 401


async def test_get_without_session_is_401(app_and_broker: FastAPI) -> None:
    async with _client(app_and_broker, raw_token=None) as client:
        response = await client.get("/api/v1/platform-apps")

    assert response.status_code == 401


async def test_put_without_confirmation_is_428(
    app_and_broker: FastAPI, owner: _SeededOwner
) -> None:
    async with _client(app_and_broker, raw_token=owner.raw_token) as client:
        response = await client.put("/api/v1/platform-apps/google", json=_VALID_GOOGLE_BODY)

    assert response.status_code == 428
    assert response.json()["error"]["code"] == "CONFIRMATION_REQUIRED"


async def test_put_with_invalid_client_id_shape_is_422(
    app_and_broker: FastAPI, owner: _SeededOwner
) -> None:
    invalid_body = {**_VALID_GOOGLE_BODY, "client_id": "not-a-valid-client-id"}
    async with _client(app_and_broker, raw_token=owner.raw_token) as client:
        response = await confirmed_request(
            client, "PUT", "/api/v1/platform-apps/google", json=invalid_body
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_delete_requires_typed_confirmation(
    app_and_broker: FastAPI, owner: _SeededOwner
) -> None:
    async with _client(app_and_broker, raw_token=owner.raw_token) as client:
        await confirmed_request(
            client, "PUT", "/api/v1/platform-apps/google", json=_VALID_GOOGLE_BODY
        )

        wrong_confirmation = await client.request(
            "DELETE", "/api/v1/platform-apps/google", json={"typed_confirmation": "nope"}
        )
        assert wrong_confirmation.status_code == 428

        deleted = await client.request(
            "DELETE", "/api/v1/platform-apps/google", json={"typed_confirmation": "ELIMINAR"}
        )
        assert deleted.status_code == 204

        status_response = await client.get("/api/v1/platform-apps")
    body = status_response.json()
    google_status = next(item for item in body["items"] if item["platform"] == "google")
    assert google_status["configured"] is False


async def test_delete_without_session_is_401(app_and_broker: FastAPI) -> None:
    async with _client(app_and_broker, raw_token=None) as client:
        response = await client.request(
            "DELETE", "/api/v1/platform-apps/google", json={"typed_confirmation": "ELIMINAR"}
        )

    assert response.status_code == 401
