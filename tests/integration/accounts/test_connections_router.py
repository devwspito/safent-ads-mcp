"""`connections_router.py` de punta a punta: Postgres real (autorizacion +
`sql_repositories.py`/`sql_connect_repositories.py`) y un broker OAuth real
sobre un socket Unix real (mismo patron que
`tests/unit/accounts/infrastructure/test_oauth_broker_client.py`) -- solo
el HTTP saliente a Google/Meta esta mockeado.

`current_owner` se sustituye por un doble (mismo patron que
`tests/unit/panel/presentation/test_rest.py::client_scoped_to_business_a`):
no vuelve a probar el login de `iam` (ya tiene su propia suite), prueba que
ESTE router llama de verdad a `require_business_access` en las tres rutas
que lo declaran -- threat-model.md C-27, "dependencia FastAPI en cada
router, no disciplina por handler"."""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.accounts.infrastructure.oauth_broker_client import OAuthBrokerSocketClient
from safent_ads.accounts.presentation.connections_router import build_connections_router
from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.platforms.google_oauth_adapter import (
    GoogleOAuthAdapter,
    GoogleOAuthAdapterConfig,
)
from safent_ads.broker.platforms.meta_oauth_adapter import MetaOAuthAdapter, MetaOAuthAdapterConfig
from safent_ads.broker.presentation.dispatcher import BrokerRuntime
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.composition.container import Container
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.presentation.dependencies import AuthenticatedOwner, current_owner
from safent_ads.shared.clock import SystemClock
from tests.conftest import BusinessFactory, OwnerFactory

pytestmark = pytest.mark.integration

_KEY_B64 = base64.b64encode(b"0" * 32).decode()
_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_META_SYSTEM_USER_TOKEN = "pasted-system-user-token"  # noqa: S105

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
_GOOGLE_LIST_CUSTOMERS_URL = (
    "https://googleads.googleapis.com/v25/customers:listAccessibleCustomers"
)
_GOOGLE_SEARCH_URL = "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:search"
_META_ME_URL = "https://graph.facebook.com/v26.0/me"
_META_ADACCOUNTS_URL = "https://graph.facebook.com/v26.0/me/adaccounts"


class _ScriptedHttpClient:
    """Doble de `OAuthHttpClient`: mismas respuestas fijas que
    `test_oauth_socket_ops.py`, recortadas a lo que este router ejercita."""

    async def post_form(self, url: str, *, data: Mapping[str, str]) -> Mapping[str, Any]:  # noqa: ARG002
        assert url == _GOOGLE_TOKEN_URL
        return {
            "refresh_token": "1//refresh",  # noqa: S106
            "access_token": "google-access-token",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/adwords",
        }

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,  # noqa: ARG002
        params: Mapping[str, str] | None = None,  # noqa: ARG002
    ) -> Mapping[str, Any]:
        if url == _GOOGLE_LIST_CUSTOMERS_URL:
            return {"resourceNames": ["customers/1234567890"]}
        if url == _META_ME_URL:
            return {"id": "10000000"}
        if url == _META_ADACCOUNTS_URL:
            return {
                "data": [
                    {
                        "account_id": "act_555",
                        "name": "Cuenta Meta",
                        "currency": "EUR",
                        "timezone_name": "Europe/Madrid",
                    }
                ]
            }
        raise AssertionError(f"url inesperada: {url}")

    async def post_json(
        self,
        url: str,
        *,
        json_body: Mapping[str, Any],  # noqa: ARG002
        headers: Mapping[str, str] | None = None,  # noqa: ARG002
    ) -> Mapping[str, Any]:
        assert url == _GOOGLE_SEARCH_URL
        return {
            "results": [
                {
                    "customer": {
                        "currencyCode": "EUR",
                        "timeZone": "Europe/Madrid",
                        "descriptiveName": "Cliente Google",
                    }
                }
            ]
        }


def _broker_runtime(store_dir: Path) -> BrokerRuntime:
    # `SystemClock`, no `FixedClock`: `Container.build` (ads-api) siempre usa
    # `SystemClock` para comprobar la caducidad de la sesion
    # (`CompleteOAuthConnect._require_valid_session`) -- un reloj fijo aqui
    # y uno real alli hace que toda sesion nazca "caducada".
    clock = SystemClock()
    http = _ScriptedHttpClient()
    store = EncryptedCredentialStore(store_dir, _KEY_B64)
    google = GoogleOAuthAdapter(
        GoogleOAuthAdapterConfig(client_id="c", client_secret="s"), http, clock
    )
    meta = MetaOAuthAdapter(MetaOAuthAdapterConfig(app_id="a", app_secret="s"), http, clock)
    oauth_flow = OAuthConnectFlow(store, google, meta, clock)
    return BrokerRuntime(
        adapters=PlatformAdapterRegistry({}),
        oauth_flow=oauth_flow,
        app_credentials=AppCredentialsService(store, clock),
    )


def _settings(*, database_url: str, broker_socket_path: Path) -> ApiSettings:
    return ApiSettings(
        database_url=database_url,
        session_secret="test-session-secret-0123456789abcdef",
        totp_enc_key=_VALID_32_BYTE_KEY_B64,
        mcp_token="test-mcp-token-abc123",
        broker_socket_path=broker_socket_path,
        public_base_url="https://ads.test.ts.net",
        telegram_bot_token="123456:test-bot-token",
        telegram_owner_chat_ids=[111222333],
        approval_signing_key=_VALID_32_BYTE_KEY_B64,
        seat_authority_enabled=True,
        enterprise_origin="https://enterprise.test",
        enterprise_service_secret="a" * 64,
        enterprise_org_ids=frozenset({uuid.UUID("00000000-0000-0000-0000-000000000001")}),
    )


@pytest.fixture
async def connections_app(
    database_url: str, tmp_path: Path
) -> AsyncIterator[tuple[FastAPI, Container]]:
    socket_path = tmp_path / "broker.sock"
    server = await serve(
        socket_path, _broker_runtime(tmp_path / "credentials"), frozenset({os.getuid()})
    )
    settings = _settings(database_url=database_url, broker_socket_path=socket_path)
    container = Container.build(settings)

    app = FastAPI()
    app.state.container = container
    app.include_router(build_connections_router(settings))

    try:
        yield app, container
    finally:
        server.close()
        await server.wait_closed()
        await container.aclose()


def _as_owner(app: FastAPI, owner_id: uuid.UUID) -> None:
    async def _fake_current_owner() -> AuthenticatedOwner:
        return AuthenticatedOwner(owner_id=owner_id, email="owner@example.com")

    app.dependency_overrides[current_owner] = _fake_current_owner


@pytest.fixture
async def client(connections_app: tuple[FastAPI, Container]) -> AsyncIterator[httpx.AsyncClient]:
    """`httpx.AsyncClient` sobre `ASGITransport`, no `TestClient`: el motor
    async de `container` (asyncpg) queda atado al loop en el que se toca
    por primera vez (tests/conftest.py: "un AsyncEngine... queda atado al
    loop en el que nace") -- `TestClient` abre su propio loop en un hilo
    aparte (`anyio` portal) y lo revienta con "attached to a different
    loop". Toda esta suite corre en el loop de `pytest-asyncio`, sin hilos."""
    app, _container = connections_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def owner_id(connections_app: tuple[FastAPI, Container]) -> uuid.UUID:
    """`OwnerFactory`/`db_session` (tests/conftest.py) envuelven cada test
    en una transaccion que se deshace al terminar -- invisible para la
    app bajo prueba, que abre SUS PROPIAS sesiones via
    `container.session_factory()` (otra conexion). Aqui se siembra con esa
    misma fabrica, para que el `commit()` sea de verdad."""
    _app, container = connections_app
    async with container.session_factory() as session:
        owner = await OwnerFactory(session).create()
        await session.commit()
    return owner


@pytest.fixture
async def business_id(connections_app: tuple[FastAPI, Container]) -> uuid.UUID:
    _app, container = connections_app
    async with container.session_factory() as session:
        business = await BusinessFactory(session).create()
        await session.commit()
    return business


async def test_idor_sweep_rejects_unauthenticated_and_unknown_resources(
    client: httpx.AsyncClient, business_id: uuid.UUID
) -> None:
    unauthenticated_cases = [
        (
            "POST",
            "/api/v1/platform-accounts/google/reconnect/start",
            {"business_id": str(business_id)},
        ),
        (
            "GET",
            "/api/v1/platform-accounts/google/reconnect/status",
            {"business_id": str(business_id), "session_id": str(uuid.uuid4())},
        ),
        (
            "POST",
            "/api/v1/platform-accounts/meta/system-user-token",
            {"business_id": str(business_id)},
        ),
        ("POST", "/api/v1/platform-accounts/google:doesnotexist/revoke", {}),
    ]
    for method, path, params in unauthenticated_cases:
        response = await client.request(method, path, params=params)
        assert response.status_code == 401, f"{method} {path} -> {response.status_code}"


async def test_idor_sweep_rejects_unknown_business_once_authenticated(
    client: httpx.AsyncClient, connections_app: tuple[FastAPI, Container], owner_id: uuid.UUID
) -> None:
    app, _container = connections_app
    _as_owner(app, owner_id)
    unknown_business = uuid.uuid4()

    start = await client.post(
        "/api/v1/platform-accounts/google/reconnect/start",
        params={"business_id": str(unknown_business)},
    )
    status_resp = await client.get(
        "/api/v1/platform-accounts/google/reconnect/status",
        params={"business_id": str(unknown_business), "session_id": str(uuid.uuid4())},
    )
    meta_token = await client.post(
        "/api/v1/platform-accounts/meta/system-user-token",
        params={"business_id": str(unknown_business)},
        json={"token": _META_SYSTEM_USER_TOKEN},
    )
    revoke = await client.post("/api/v1/platform-accounts/google:doesnotexist/revoke")

    assert start.status_code == 404
    assert status_resp.status_code == 404
    assert meta_token.status_code == 404
    assert revoke.status_code == 404
    for response in (start, status_resp, meta_token, revoke):
        assert "Traceback" not in response.text


async def test_revoke_rejects_malformed_account_id(
    client: httpx.AsyncClient, connections_app: tuple[FastAPI, Container], owner_id: uuid.UUID
) -> None:
    app, _container = connections_app
    _as_owner(app, owner_id)

    response = await client.post("/api/v1/platform-accounts/not-a-valid-ref/revoke")

    assert response.status_code == 422


async def test_google_connect_round_trip_then_revoke(
    client: httpx.AsyncClient,
    connections_app: tuple[FastAPI, Container],
    owner_id: uuid.UUID,
    business_id: uuid.UUID,
) -> None:
    app, _container = connections_app
    _as_owner(app, owner_id)

    start = await client.post(
        "/api/v1/platform-accounts/google/reconnect/start",
        params={"business_id": str(business_id)},
    )
    assert start.status_code == 201
    body = start.json()
    assert set(body) == {"session_id", "authorize_url", "expires_at"}

    waiting = await client.get(
        "/api/v1/platform-accounts/google/reconnect/status",
        params={"business_id": str(business_id), "session_id": body["session_id"]},
    )
    assert waiting.status_code == 200
    assert waiting.json()["state"] == "waiting"

    callback = await client.get(
        "/api/v1/platform-accounts/google/reconnect/callback",
        params={"state": _state_from_authorize_url(body["authorize_url"]), "code": "auth-code"},
    )
    assert callback.status_code == 200
    assert "cerrar esta ventana" in callback.text

    resolved = await client.get(
        "/api/v1/platform-accounts/google/reconnect/status",
        params={"business_id": str(business_id), "session_id": body["session_id"]},
    )
    assert resolved.status_code == 200
    assert resolved.json()["state"] == "ok"

    accounts = await client.get(
        "/api/v1/platform-accounts", params={"business_id": str(business_id)}
    )
    account_ref = accounts.json()["items"][0]["platform_account_id"]
    revoke = await client.post(f"/api/v1/platform-accounts/{account_ref}/revoke")
    assert revoke.status_code == 204

    revoke_again = await client.post(f"/api/v1/platform-accounts/{account_ref}/revoke")
    assert revoke_again.status_code == 409


async def test_empty_inventory_callback_persists_actionable_error_not_connected(
    client, connections_app, owner_id, business_id, monkeypatch,
) -> None:
    app, container = connections_app
    _as_owner(app, owner_id)
    original_get = _ScriptedHttpClient.get_json
    inventory_calls = 0

    async def empty_inventory(self, url, **kwargs):
        nonlocal inventory_calls
        if url == _GOOGLE_LIST_CUSTOMERS_URL:
            inventory_calls += 1
            return {"resourceNames": []}
        return await original_get(self, url, **kwargs)

    monkeypatch.setattr(_ScriptedHttpClient, "get_json", empty_inventory)
    start = (await client.post(
        "/api/v1/platform-accounts/google/reconnect/start",
        params={"business_id": str(business_id)},
    )).json()
    state = _state_from_authorize_url(start["authorize_url"])
    callback_params = {"state": state, "code": "synthetic-auth-code"}
    for _ in range(2):  # A replay cannot change the terminal result or exchange again.
        callback = await client.get(
            "/api/v1/platform-accounts/google/reconnect/callback", params=callback_params,
        )
        assert callback.status_code == 200
        resolved = await client.get(
            "/api/v1/platform-accounts/google/reconnect/status",
            params={"business_id": str(business_id), "session_id": start["session_id"]},
        )
        assert resolved.status_code == 200
        assert resolved.json() == {
            "state": "error", "error_code": "OAUTH_NO_ACCESSIBLE_ACCOUNTS",
            "message": (
                "No se encontró ninguna cuenta publicitaria accesible. Vuelve a conectar y "
                "autoriza al menos una cuenta publicitaria a la que tengas acceso."
            ),
        }
    assert inventory_calls == 1
    accounts = await client.get(
        "/api/v1/platform-accounts", params={"business_id": str(business_id)},
    )
    assert accounts.json()["items"] == []
    async with container.session_factory() as db_session:
        row = (await db_session.execute(
            text("SELECT status,error_code FROM oauth_connect_sessions WHERE id=:id"),
            {"id": uuid.UUID(start["session_id"])},
        )).mappings().one()
        assert row["status"] == "error"
        assert row["error_code"] == "OAUTH_NO_ACCESSIBLE_ACCOUNTS"


async def test_background_inventory_is_waiting_until_accounts_are_committed(
    client, connections_app, owner_id, business_id, monkeypatch,
) -> None:
    app, _container = connections_app
    _as_owner(app, owner_id)
    start = (await client.post(
        "/api/v1/platform-accounts/google/reconnect/start",
        params={"business_id": str(business_id)},
    )).json()
    entered, release = asyncio.Event(), asyncio.Event()
    original = _ScriptedHttpClient.post_json

    async def held_inventory(self, url, *, json_body, headers=None):
        entered.set()
        await release.wait()
        return await original(self, url, json_body=json_body, headers=headers)

    monkeypatch.setattr(_ScriptedHttpClient, "post_json", held_inventory)
    callback = asyncio.create_task(client.get(
        "/api/v1/platform-accounts/google/reconnect/callback",
        params={"state": _state_from_authorize_url(start["authorize_url"]), "code": "auth-code"},
    ))
    status_params = {"business_id": str(business_id), "session_id": start["session_id"]}
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        waiting = await client.get(
            "/api/v1/platform-accounts/google/reconnect/status", params=status_params,
        )
        assert waiting.json()["state"] == "waiting"
        inventory = await client.get(
            "/api/v1/platform-accounts", params={"business_id": str(business_id)},
        )
        assert inventory.json()["items"] == []
    finally:
        release.set()
        assert (await asyncio.wait_for(callback, timeout=3)).status_code == 200
    completed = await client.get(
        "/api/v1/platform-accounts/google/reconnect/status", params=status_params,
    )
    assert completed.json()["state"] == "ok"
    inventory = await client.get(
        "/api/v1/platform-accounts", params={"business_id": str(business_id)},
    )
    assert len(inventory.json()["items"]) == 1


async def test_socket_failure_is_persisted_and_a_fresh_attempt_can_succeed(
    client, connections_app, owner_id, business_id, monkeypatch,
) -> None:
    app, container = connections_app
    _as_owner(app, owner_id)
    original = OAuthBrokerSocketClient.complete
    calls = 0

    async def unavailable_once(self, state, code):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BrokerConnectionError("synthetic private detail")
        return await original(self, state, code)

    monkeypatch.setattr(OAuthBrokerSocketClient, "complete", unavailable_once)
    for expected in ("error", "ok"):
        start = (await client.post(
            "/api/v1/platform-accounts/google/reconnect/start",
            params={"business_id": str(business_id)},
        )).json()
        callback = await client.get(
            "/api/v1/platform-accounts/google/reconnect/callback",
            params={
                "state": _state_from_authorize_url(start["authorize_url"]), "code": "auth-code",
            },
        )
        assert callback.status_code == 200
        result = await client.get(
            "/api/v1/platform-accounts/google/reconnect/status",
            params={"business_id": str(business_id), "session_id": start["session_id"]},
        )
        assert result.json()["state"] == expected
        assert "synthetic private detail" not in result.text
        if expected == "error":
            assert result.json()["error_code"] == "BROKER_UNAVAILABLE"
            async with container.session_factory() as db:
                row = (await db.execute(
                    text("SELECT status FROM oauth_connect_sessions WHERE id=:id"),
                    {"id": uuid.UUID(start["session_id"])},
                )).scalar_one()
                assert row == "error"


async def test_polling_expires_abandoned_callback_in_sql(
    client, connections_app, owner_id, business_id,
) -> None:
    app, container = connections_app
    _as_owner(app, owner_id)
    start = (await client.post(
        "/api/v1/platform-accounts/google/reconnect/start",
        params={"business_id": str(business_id)},
    )).json()
    async with container.session_factory() as db:
        await db.execute(
            text("UPDATE oauth_connect_sessions SET expires_at=now()-interval '1 second' "
                 "WHERE id=:id"), {"id": uuid.UUID(start["session_id"])},
        )
        await db.commit()
    expired = await client.get(
        "/api/v1/platform-accounts/google/reconnect/status",
        params={"business_id": str(business_id), "session_id": start["session_id"]},
    )
    assert expired.json()["state"] == "error"
    assert expired.json()["error_code"] == "OAUTH_SESSION_EXPIRED"
    async with container.session_factory() as db:
        row = (await db.execute(
            text("SELECT status,error_code,completed_at FROM oauth_connect_sessions WHERE id=:id"),
            {"id": uuid.UUID(start["session_id"])},
        )).mappings().one()
        assert row["status"] == "error"
        assert row["error_code"] == "OAUTH_SESSION_EXPIRED"
        assert row["completed_at"] is not None


async def test_external_callback_provider_expiry_replay_and_concurrency(
    client: httpx.AsyncClient,
    connections_app: tuple[FastAPI, Container],
    owner_id: uuid.UUID,
    business_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, container = connections_app
    _as_owner(app, owner_id)
    start = await client.post(
        "/api/v1/platform-accounts/google/reconnect/start", params={"business_id": str(business_id)}
    )
    state = _state_from_authorize_url(start.json()["authorize_url"])
    calls = []
    original = _ScriptedHttpClient.post_form

    async def counted(self, url, *, data):
        calls.append(url)
        await asyncio.sleep(0.05)
        return await original(self, url, data=data)

    monkeypatch.setattr(_ScriptedHttpClient, "post_form", counted)
    app.dependency_overrides.clear()  # No owner/browser session on the external callback.
    wrong = await client.get(
        "/api/v1/platform-accounts/meta/reconnect/callback",
        params={"state": state, "code": "auth-code"},
    )
    assert wrong.status_code == 200 and calls == []
    responses = await asyncio.gather(
        *[
            client.get(
                "/api/v1/platform-accounts/google/reconnect/callback",
                params={"state": state, "code": "auth-code"},
            )
            for _ in range(2)
        ]
    )
    assert all(r.status_code == 200 and r.headers["cache-control"] == "no-store" for r in responses)
    assert len(calls) == 1
    async with container.session_factory() as session:
        row = (
            await session.execute(
                text("SELECT status FROM oauth_connect_sessions WHERE id=:id"),
                {"id": uuid.UUID(start.json()["session_id"])},
            )
        ).scalar_one()
        assert row in {"ok", "OK"}
    await client.get(
        "/api/v1/platform-accounts/google/reconnect/callback",
        params={"state": state, "code": "auth-code"},
    )
    assert len(calls) == 1

    _as_owner(app, owner_id)
    expired = await client.post(
        "/api/v1/platform-accounts/google/reconnect/start", params={"business_id": str(business_id)}
    )
    async with container.session_factory() as session:
        await session.execute(
            text(
                "UPDATE oauth_connect_sessions SET expires_at=now()-interval '1 second' "
                "WHERE id=:id"
            ),
            {"id": uuid.UUID(expired.json()["session_id"])},
        )
        await session.commit()
    app.dependency_overrides.clear()
    await client.get(
        "/api/v1/platform-accounts/google/reconnect/callback",
        params={
            "state": _state_from_authorize_url(expired.json()["authorize_url"]),
            "code": "auth-code",
        },
    )
    assert len(calls) == 1


async def test_register_meta_system_user_token_never_echoes_the_pasted_token(
    client: httpx.AsyncClient,
    connections_app: tuple[FastAPI, Container],
    owner_id: uuid.UUID,
    business_id: uuid.UUID,
) -> None:
    app, _container = connections_app
    _as_owner(app, owner_id)

    response = await client.post(
        "/api/v1/platform-accounts/meta/system-user-token",
        params={"business_id": str(business_id)},
        json={"token": _META_SYSTEM_USER_TOKEN},
    )

    assert response.status_code == 201
    assert _META_SYSTEM_USER_TOKEN not in response.text
    accounts = response.json()["accounts"]
    assert accounts[0]["external_account_id"] == "act_555"


def _state_from_authorize_url(authorize_url: str) -> str:
    query = parse_qs(urlparse(authorize_url).query)
    return query["state"][0]
