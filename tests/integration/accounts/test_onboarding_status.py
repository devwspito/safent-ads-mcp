"""`GET /api/v1/onboarding` de punta a punta (029 T022, contracts/rest-api.md
§Onboarding): Postgres real (sesion + `platform_accounts`) y un bróker real
sobre un socket Unix real (mismo patrón que `test_platform_apps_router.py`)
-- prueba que el estado agregado refleja `/platform-apps` + cuentas
conectadas de verdad, y que ninguna credencial se cuela en la respuesta.

`exists_for_platform` es GLOBAL por diseño (estado de toda la instalación,
igual que `AccountLinkStatusPort.linked_platforms` de `GET /mcp/health`):
corre sobre `isolated_database_url` (`tests/conftest.py`), no sobre la base
compartida -- una fila que dejara otro test cambiaría el resultado sin que
el código tenga nada que ver (mismo criterio que
`tests/integration/mcp/test_sql_health_ports.py`)."""

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

from safent_ads.accounts.presentation.onboarding_rest import build_onboarding_router
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


async def _seed_owner(session: AsyncSession) -> _SeededOwner:
    owner_id = uuid.uuid4()
    raw_token = f"onboarding-test-token-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    encrypted_secret = AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64).encrypt(
        _TOTP_SECRET, purpose=PURPOSE_TOTP_SECRET
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
            "now": now,
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


@pytest.fixture
async def owner(isolated_database_url: str) -> AsyncIterator[_SeededOwner]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        seeded = await _seed_owner(session)
        await session.commit()
    try:
        yield seeded
    finally:
        # Solo `sessions`: `owners` puede tener `totp_reauth_confirmations`
        # colgando de un `PUT /platform-apps` con reauth (FK), y ninguna
        # consulta de este endpoint es global sobre `owners` -- mismo
        # criterio de limpieza parcial que `test_platform_apps_router.py`.
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(seeded.owner_id)}
            )
        await engine.dispose()


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
async def app_and_broker(isolated_database_url: str, tmp_path: Path) -> AsyncIterator[FastAPI]:
    socket_path = tmp_path / "broker.sock"
    server = await serve(
        socket_path, _broker_runtime(tmp_path / "credentials"), frozenset({os.getuid()})
    )
    settings = build_api_settings(
        database_url=isolated_database_url, broker_socket_path=socket_path
    )
    container = Container.build(settings)

    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_platform_apps_router(settings))
    app.include_router(build_onboarding_router(settings))

    try:
        yield app
    finally:
        server.close()
        await server.wait_closed()
        await container.aclose()


def _client(app: FastAPI, *, raw_token: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: raw_token, "ads_csrf": "test-csrf"},
        headers={"X-CSRF-Token": "test-csrf"},
    )


@pytest.fixture
async def connected_google_account(isolated_database_url: str) -> AsyncIterator[None]:
    """Una `platform_accounts` de Google, sin credencial de cliente real
    (`credential_ref_id` nulo, igual que `test_sql_health_ports.py`
    `committed_google_account`): a `exists_for_platform` le basta la fila,
    nunca lee el token."""
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    business_id = uuid.uuid4()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio de onboarding', 'Europe/Madrid', 'EUR')"
            ),
            {"id": str(business_id), "slug": f"onboarding-fixture-{business_id.hex[:12]}"},
        )
        await session.execute(
            text(
                "INSERT INTO platform_accounts "
                "(business_id, platform, external_account_id, currency, timezone, "
                " api_tier, status) "
                "VALUES (:business_id, 'google', :external_id, 'EUR', 'Europe/Madrid', "
                " 'google_standard', 'ACTIVE')"
            ),
            {"business_id": str(business_id), "external_id": f"act_{business_id.hex[:8]}"},
        )
        await session.commit()
    try:
        yield
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM platform_accounts WHERE business_id = :id"),
                {"id": str(business_id)},
            )
            await connection.execute(
                text("DELETE FROM businesses WHERE id = :id"), {"id": str(business_id)}
            )
        await engine.dispose()


async def test_response_always_has_all_four_steps_with_a_well_formed_status(
    app_and_broker: FastAPI, owner: _SeededOwner
) -> None:
    """Forma del contrato, no la rama de negocio: `isolated_database_url`
    la comparten ~40 ficheros de test que tambien crean `platform_accounts`
    (`test_rules_and_platform_accounts_routes.py` entre otros), asi que el
    estado exacto (`blocked` vs `pending` vs `done`) de los pasos de cuenta
    NO es deterministico aqui -- esa matriz de transiciones la prueba
    `tests/unit/accounts/application/test_get_onboarding_status.py` con
    `InMemoryAccountRepository`, sin ese ruido."""
    async with _client(app_and_broker, raw_token=owner.raw_token) as client:
        response = await client.get("/api/v1/onboarding")

    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["complete"], bool)
    ids = {step["id"] for step in body["steps"]}
    assert ids == {"google_app", "google_account", "meta_app", "meta_account"}
    for step in body["steps"]:
        assert step["status"] in {"done", "pending", "blocked"}
        if step["status"] != "blocked":
            assert step["blocking_reason"] is None


async def test_google_app_step_follows_the_broker_state_not_postgres(
    app_and_broker: FastAPI, owner: _SeededOwner
) -> None:
    """`google_app`/`meta_app` leen el almacén cifrado del bróker bajo
    `tmp_path` (nuevo por función de test, nunca compartido) -- a
    diferencia de los pasos de cuenta, esta transición sí es determinista
    aquí en ambos sentidos."""
    async with _client(app_and_broker, raw_token=owner.raw_token) as client:
        before = await client.get("/api/v1/onboarding")
        before_by_id = {step["id"]: step for step in before.json()["steps"]}
        assert before_by_id["google_app"]["status"] == "pending"

        put_response = await confirmed_request(
            client, "PUT", "/api/v1/platform-apps/google", json=_VALID_GOOGLE_BODY
        )
        assert put_response.status_code == 200, put_response.text
        after = await client.get("/api/v1/onboarding")

    after_by_id = {step["id"]: step for step in after.json()["steps"]}
    assert after_by_id["google_app"]["status"] == "done"
    # Configurada la app, `google_account` ya nunca puede estar bloqueado
    # por esta razón -- independiente de si Postgres tiene o no una cuenta
    # conectada de otro test.
    assert after_by_id["google_account"]["blocking_reason"] is None


async def test_connecting_an_account_completes_onboarding(
    app_and_broker: FastAPI, owner: _SeededOwner, connected_google_account: None
) -> None:
    """Monótono, así que es seguro de afirmar pese al ruido de otros
    tests: una cuenta que ESTE test conecta siempre deja su paso `done` y
    `complete` en `true`, la haya o no otra cuenta conectada ya."""
    async with _client(app_and_broker, raw_token=owner.raw_token) as client:
        response = await client.get("/api/v1/onboarding")

    body = response.json()
    by_id = {step["id"]: step for step in body["steps"]}
    assert by_id["google_account"]["status"] == "done"
    assert by_id["google_account"]["blocking_reason"] is None
    assert body["complete"] is True


async def test_response_never_leaks_the_client_secret(
    app_and_broker: FastAPI, owner: _SeededOwner
) -> None:
    async with _client(app_and_broker, raw_token=owner.raw_token) as client:
        await confirmed_request(
            client, "PUT", "/api/v1/platform-apps/google", json=_VALID_GOOGLE_BODY
        )
        response = await client.get("/api/v1/onboarding")

    assert _VALID_GOOGLE_BODY["client_secret"] not in response.text


async def test_get_without_session_is_401(app_and_broker: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app_and_broker)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/onboarding")

    assert response.status_code == 401
