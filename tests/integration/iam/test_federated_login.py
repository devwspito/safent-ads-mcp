"""`federated_router.py` (spec 002b tasks.md T030, contracts/federated-login.md
§1) against real Postgres, provider doubled with `FakeFederatedIdentityProvider`
(T024). Router mount isolated (FastAPI empty), same pattern as
`tests/integration/mcp_oauth/test_consent_router.py`: the app wiring
(`composition/api.py`, tasks.md T033) is NOT part of this lane.

Covers: clean install without an owner (bootstrap TOFU); a second login
recognising the same owner without a duplicate; unverified/disallowed
email collapsing into the same `denied` (SC-103); replayed/unknown/expired
`state` all answering `expired` with zero effects; a provider failure
answering `provider_unavailable`; the callback always answering `303` and
never reflecting anything from the query string.

T053 (US2) adds the re-identification path: `federated_router.py` mounted
together with `consent_router.py` (`_build_full_app`) to exercise stale
freshness -> `401` -> `start` (purpose `reidentify`) -> `callback` ->
the SAME pending consent transaction stays alive and completes (FR-113),
including the fix for checkpoints/us1.md's open item -- a FAILED
re-identification callback now returns to that same consent screen
(`/oauth/autorizar?txn=...&federated_error=<code>`), never to `/login`."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
import structlog.testing
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.federated_routes import _SqlOpenConsentTransactions
from safent_ads.iam.application.ports import AuthorizedEmailList
from safent_ads.iam.domain.email import Email
from safent_ads.iam.domain.federated_identity import FederatedSubject
from safent_ads.iam.domain.federated_transaction import ReferenceHash
from safent_ads.iam.infrastructure.fakes import FakeFederatedIdentityProvider
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.federated_router import build_federated_router
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.mcp_oauth.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRequestRepository,
)
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import SqlClientRepository
from safent_ads.mcp_oauth.presentation.consent_router import build_consent_router
from safent_ads.shared.clock import SystemClock

pytestmark = pytest.mark.integration

_PUBLIC_BASE_URL = "https://ads.test.ts.net"
_REDIRECT_URI = f"{_PUBLIC_BASE_URL}/api/v1/auth/federated/callback"
_ALLOWED_EMAIL = "dueno@example.com"
_CALLBACK_PATH = "/api/v1/auth/federated/callback"
_START_PATH = "/api/v1/auth/federated/start"
_STATUS_PATH = "/api/v1/auth/federated/status"

_CLEANUP = (
    "DELETE FROM federated_login_transactions",
    "DELETE FROM oauth_authorization_requests",
    "DELETE FROM oauth_clients",
    "DELETE FROM owner_federated_identities",
    "DELETE FROM sessions",
    "DELETE FROM owners",
    "DELETE FROM login_attempts",
)


async def _wipe(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        for statement in _CLEANUP:
            await connection.execute(text(statement))


@pytest.fixture(autouse=True)
async def _clean_database(isolated_iam_database_url: str) -> AsyncIterator[None]:
    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    await _wipe(engine)
    try:
        yield
    finally:
        await _wipe(engine)
        await engine.dispose()


def _build_app(
    isolated_iam_database_url: str,
    provider: FakeFederatedIdentityProvider,
    *,
    trusted_proxy_hops: int = 0,
) -> tuple[FastAPI, Container]:
    settings = build_api_settings(
        database_url=isolated_iam_database_url,
        public_base_url=_PUBLIC_BASE_URL,
        trusted_proxy_hops=trusted_proxy_hops,
    )
    container = Container.build(settings)
    fastapi_app = FastAPI()
    fastapi_app.state.container = container
    fastapi_app.add_exception_handler(ApiError, _handle_api_error)
    fastapi_app.include_router(
        build_federated_router(
            provider=provider,
            redirect_uri=_REDIRECT_URI,
            allowed_emails=AuthorizedEmailList.from_raw((_ALLOWED_EMAIL,)),
            open_consent_transactions=_SqlOpenConsentTransactions(container),
            trusted_proxy_hops=settings.trusted_proxy_hops,
        )
    )
    return fastapi_app, container


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


def _extract_state(authorization_url: str) -> str:
    query = parse_qs(urlsplit(authorization_url).query)
    return query["state"][0]


async def _start(http_client: httpx.AsyncClient, *, txn_id: UUID | None = None) -> str:
    response = await http_client.post(_START_PATH, json={"txn_id": str(txn_id) if txn_id else None})
    assert response.status_code == 200, response.text
    return _extract_state(response.json()["authorization_url"])


async def _seed_pending_authorization_request(isolated_iam_database_url: str) -> UUID:
    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    client = OAuthClient(
        client_id=f"client-federated-test-{uuid4().hex[:8]}",
        client_name="Claude Code",
        redirect_uris=(RedirectUri("http://127.0.0.1:54321/callback"),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read ads:propose"),
        created_at=datetime.now(UTC),
    )
    now = datetime.now(UTC)
    request = AuthorizationRequest(
        txn_id=uuid4(),
        client_id=client.id,
        redirect_uri="http://127.0.0.1:54321/callback",
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        client_state="xyz",
        scope_set=ScopeSet.parse("ads:read ads:propose"),
        resource=ResourceIndicator(f"{_PUBLIC_BASE_URL}/mcp"),
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await SqlClientRepository(session).save(client)
        await SqlAuthorizationRequestRepository(session, clock=SystemClock()).create(request)
        await session.commit()
    await engine.dispose()
    return request.id


async def _count(engine_url: str, table: str) -> int:
    engine = create_async_engine(engine_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        result = await connection.execute(text(f"SELECT count(*) FROM {table}"))  # noqa: S608 - literal table from a closed set below
        value = int(result.scalar_one())
    await engine.dispose()
    return value


async def _expire_transaction(isolated_iam_database_url: str, state: str) -> None:
    """Pone `expires_at` justo despues de `created_at` -- no antes: el
    `CHECK (expires_at > created_at)` de `0052` lo exige incluso para una
    fila que se va a tratar como caducada. Para cuando el test haga la
    siguiente peticion HTTP, el reloj real ya habra pasado ese instante."""
    state_hash = str(ReferenceHash.of(state))
    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        created_at = (
            await connection.execute(
                text(
                    "SELECT created_at FROM federated_login_transactions "
                    "WHERE state_hash = :state_hash"
                ),
                {"state_hash": state_hash},
            )
        ).scalar_one()
        await connection.execute(
            text(
                "UPDATE federated_login_transactions SET expires_at = :expires_at "
                "WHERE state_hash = :state_hash"
            ),
            {"expires_at": created_at + timedelta(milliseconds=1), "state_hash": state_hash},
        )
    await engine.dispose()


# ---------------------------------------------------------------------------
# Interruptor apagado: 404 por enrutado (research.md Decision F)
# ---------------------------------------------------------------------------


async def test_disabled_switch_answers_404_on_all_three_routes() -> None:
    app = FastAPI()
    app.add_exception_handler(ApiError, _handle_api_error)

    async with _client(app) as http_client:
        status_response = await http_client.get(_STATUS_PATH)
        start_response = await http_client.post(_START_PATH, json={"txn_id": None})
        callback_response = await http_client.get(f"{_CALLBACK_PATH}?state=whatever&code=x")

    assert status_response.status_code == 404
    assert start_response.status_code == 404
    assert callback_response.status_code == 404


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


async def test_status_is_200_with_no_store_when_mounted(isolated_iam_database_url: str) -> None:
    app, container = _build_app(isolated_iam_database_url, FakeFederatedIdentityProvider())
    try:
        async with _client(app) as http_client:
            response = await http_client.get(_STATUS_PATH)
    finally:
        await container.aclose()

    assert response.status_code == 200
    assert response.json() == {"available": True}
    assert response.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# /start: validacion del txn_id
# ---------------------------------------------------------------------------


async def test_start_with_an_unknown_txn_id_is_404(isolated_iam_database_url: str) -> None:
    app, container = _build_app(isolated_iam_database_url, FakeFederatedIdentityProvider())
    try:
        async with _client(app) as http_client:
            response = await http_client.post(_START_PATH, json={"txn_id": str(uuid4())})
    finally:
        await container.aclose()

    assert response.status_code == 404


async def test_pending_federated_transactions_are_capped_per_ip(
    isolated_iam_database_url: str,
) -> None:
    """threat-model.md C-79: la sexta transaccion pendiente desde la misma
    IP se rechaza SIN crearse -- mismo cuerpo de denegacion (`ACCOUNT_
    LOCKED`, 429) que el resto de `/api/v1/auth`."""
    app, container = _build_app(isolated_iam_database_url, FakeFederatedIdentityProvider())
    try:
        async with _client(app) as http_client:
            for _ in range(5):
                response = await http_client.post(_START_PATH, json={"txn_id": None})
                assert response.status_code == 200, response.text

            sixth = await http_client.post(_START_PATH, json={"txn_id": None})
    finally:
        await container.aclose()

    assert sixth.status_code == 429
    assert sixth.json()["error"]["code"] == "ACCOUNT_LOCKED"
    assert await _count(isolated_iam_database_url, "federated_login_transactions") == 5


async def test_pending_transaction_cap_is_keyed_per_forwarded_ip_with_one_trusted_hop(
    isolated_iam_database_url: str,
) -> None:
    """Code review 17-sep (B-1, item 1): detras de un proxy de confianza
    (`ADS_TRUSTED_PROXY_HOPS=1`, Caddy en produccion), el tope de C-79 se
    particiona por la IP real que el proxy anadio -- no por la IP del
    proxy, constante para todo Internet. Dos IPs distintas nunca comparten
    cupo."""
    app, container = _build_app(
        isolated_iam_database_url, FakeFederatedIdentityProvider(), trusted_proxy_hops=1
    )
    try:
        async with _client(app) as http_client:
            for _ in range(5):
                response = await http_client.post(
                    _START_PATH,
                    json={"txn_id": None},
                    headers={"X-Forwarded-For": "198.51.100.10"},
                )
                assert response.status_code == 200, response.text

            sixth_same_ip = await http_client.post(
                _START_PATH, json={"txn_id": None}, headers={"X-Forwarded-For": "198.51.100.10"}
            )
            first_other_ip = await http_client.post(
                _START_PATH, json={"txn_id": None}, headers={"X-Forwarded-For": "198.51.100.20"}
            )
    finally:
        await container.aclose()

    assert sixth_same_ip.status_code == 429
    assert first_other_ip.status_code == 200, first_other_ip.text


async def test_zero_hops_ignores_x_forwarded_for_and_caps_by_the_peer_ip(
    isolated_iam_database_url: str,
) -> None:
    """Sin proxy de confianza (`ADS_TRUSTED_PROXY_HOPS=0`, companion/conexion
    directa), `X-Forwarded-For` NUNCA se lee -- rotarlo en cada peticion no
    debe servir para esquivar el tope de C-79."""
    app, container = _build_app(
        isolated_iam_database_url, FakeFederatedIdentityProvider(), trusted_proxy_hops=0
    )
    try:
        async with _client(app) as http_client:
            for i in range(5):
                response = await http_client.post(
                    _START_PATH,
                    json={"txn_id": None},
                    headers={"X-Forwarded-For": f"203.0.113.{i}"},
                )
                assert response.status_code == 200, response.text

            sixth = await http_client.post(
                _START_PATH, json={"txn_id": None}, headers={"X-Forwarded-For": "203.0.113.99"}
            )
    finally:
        await container.aclose()

    assert sixth.status_code == 429


# ---------------------------------------------------------------------------
# Bootstrap: instalacion sin dueno
# ---------------------------------------------------------------------------


async def test_clean_install_start_then_callback_creates_owner_session_and_identity(
    isolated_iam_database_url: str,
) -> None:
    app, container = _build_app(isolated_iam_database_url, FakeFederatedIdentityProvider())
    try:
        async with _client(app) as http_client:
            state = await _start(http_client)
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    set_cookie = response.headers["set-cookie"]
    assert "ads_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "samesite=strict" in set_cookie.lower()
    assert "path=/api" in set_cookie.lower()

    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        owner_row = (
            (
                await connection.execute(
                    text("SELECT id, email FROM owners WHERE email = :email"),
                    {"email": _ALLOWED_EMAIL},
                )
            )
            .mappings()
            .one()
        )
        session_row = (
            (
                await connection.execute(
                    text(
                        "SELECT origin, last_federated_auth_at FROM sessions "
                        "WHERE owner_id = :owner_id"
                    ),
                    {"owner_id": str(owner_row["id"])},
                )
            )
            .mappings()
            .one()
        )
        identity_row = (
            (
                await connection.execute(
                    text(
                        "SELECT subject FROM owner_federated_identities WHERE owner_id = :owner_id"
                    ),
                    {"owner_id": str(owner_row["id"])},
                )
            )
            .mappings()
            .one()
        )
    await engine.dispose()

    assert session_row["origin"] == "federated"
    assert session_row["last_federated_auth_at"] is not None
    assert identity_row["subject"]


async def test_success_with_a_txn_id_lands_on_that_same_consent_screen(
    isolated_iam_database_url: str,
) -> None:
    txn_id = await _seed_pending_authorization_request(isolated_iam_database_url)
    app, container = _build_app(isolated_iam_database_url, FakeFederatedIdentityProvider())
    try:
        async with _client(app) as http_client:
            state = await _start(http_client, txn_id=txn_id)
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert response.headers["location"] == f"/oauth/autorizar?txn={txn_id}"


async def test_second_login_recognises_the_same_owner_without_a_duplicate(
    isolated_iam_database_url: str,
) -> None:
    provider = FakeFederatedIdentityProvider()
    app, container = _build_app(isolated_iam_database_url, provider)
    try:
        async with _client(app) as first_client:
            first_state = await _start(first_client)
            first_response = await first_client.get(
                f"{_CALLBACK_PATH}?state={first_state}&code=fake-code-1", follow_redirects=False
            )
        assert first_response.status_code == 303

        async with _client(app) as second_client:
            second_state = await _start(second_client)
            second_response = await second_client.get(
                f"{_CALLBACK_PATH}?state={second_state}&code=fake-code-2", follow_redirects=False
            )
        assert second_response.status_code == 303
    finally:
        await container.aclose()

    assert await _count(isolated_iam_database_url, "owners") == 1
    assert await _count(isolated_iam_database_url, "sessions") == 2
    assert await _count(isolated_iam_database_url, "owner_federated_identities") == 1


async def test_bootstrap_emits_owner_registered_warning_once(
    isolated_iam_database_url: str,
) -> None:
    """threat-model.md C-61/MENOR-2: el alta del primer dueno -- el acto mas
    privilegiado de la instalacion -- se audita a WARNING con `owner_id`,
    `issuer`, `email` y `at`. Una segunda entrada del mismo dueno NO repite
    el evento (`OwnerFederatedIdentityResolution.ALREADY_BOUND`, nunca
    `CREATED` dos veces)."""
    provider = FakeFederatedIdentityProvider()
    app, container = _build_app(isolated_iam_database_url, provider)
    try:
        with structlog.testing.capture_logs() as logs:
            async with _client(app) as first_client:
                first_state = await _start(first_client)
                first_response = await first_client.get(
                    f"{_CALLBACK_PATH}?state={first_state}&code=fake-code-1",
                    follow_redirects=False,
                )
            assert first_response.status_code == 303

            async with _client(app) as second_client:
                second_state = await _start(second_client)
                second_response = await second_client.get(
                    f"{_CALLBACK_PATH}?state={second_state}&code=fake-code-2",
                    follow_redirects=False,
                )
            assert second_response.status_code == 303
    finally:
        await container.aclose()

    registrations = [entry for entry in logs if entry["event"] == "federated_owner_registered"]
    assert len(registrations) == 1
    registration = registrations[0]
    assert registration["log_level"] == "warning"
    assert registration["email"] == _ALLOWED_EMAIL
    assert registration["issuer"]
    assert registration["owner_id"]
    assert registration["at"]


# ---------------------------------------------------------------------------
# Denegacion (SC-103): mismo codigo para no verificado y fuera de lista
# ---------------------------------------------------------------------------


async def test_unverified_email_is_denied_without_any_owner_or_session(
    isolated_iam_database_url: str,
) -> None:
    provider = FakeFederatedIdentityProvider(email_verified=False)
    app, container = _build_app(isolated_iam_database_url, provider)
    try:
        async with _client(app) as http_client:
            state = await _start(http_client)
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert response.headers["location"] == "/login?federated_error=denied"
    assert await _count(isolated_iam_database_url, "owners") == 0
    assert await _count(isolated_iam_database_url, "sessions") == 0


async def test_email_outside_the_allow_list_is_denied_the_same_way(
    isolated_iam_database_url: str,
) -> None:
    provider = FakeFederatedIdentityProvider(email=Email("intruso@example.com"))
    app, container = _build_app(isolated_iam_database_url, provider)
    try:
        async with _client(app) as http_client:
            state = await _start(http_client)
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert response.headers["location"] == "/login?federated_error=denied"
    assert await _count(isolated_iam_database_url, "owners") == 0


async def test_denied_records_a_login_attempt_against_the_claimed_email_only(
    isolated_iam_database_url: str,
) -> None:
    """threat-model.md C-77: el intento fallido cuenta contra el correo del
    `id_token`, nunca contra un dueno (que en este escenario ni existe)."""
    provider = FakeFederatedIdentityProvider(email=Email("intruso@example.com"))
    app, container = _build_app(isolated_iam_database_url, provider)
    try:
        async with _client(app) as http_client:
            state = await _start(http_client)
            await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        failures = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM login_attempts WHERE email = :email AND succeeded = false"
                ),
                {"email": "intruso@example.com"},
            )
        ).scalar_one()
    await engine.dispose()

    assert failures == 1


# ---------------------------------------------------------------------------
# state desconocido / caducado / repetido
# ---------------------------------------------------------------------------


async def test_unknown_state_is_expired_without_effects(isolated_iam_database_url: str) -> None:
    app, container = _build_app(isolated_iam_database_url, FakeFederatedIdentityProvider())
    try:
        async with _client(app) as http_client:
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state=never-issued&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert response.headers["location"] == "/login?federated_error=expired"
    assert await _count(isolated_iam_database_url, "owners") == 0


async def test_replayed_state_is_rejected_without_a_second_owner_or_session(
    isolated_iam_database_url: str,
) -> None:
    provider = FakeFederatedIdentityProvider()
    app, container = _build_app(isolated_iam_database_url, provider)
    try:
        async with _client(app) as http_client:
            state = await _start(http_client)
            first = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
            assert first.status_code == 303
            assert first.headers["location"] == "/"

            replay = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert replay.status_code == 303
    assert replay.headers["location"] == "/login?federated_error=expired"
    assert await _count(isolated_iam_database_url, "owners") == 1


async def test_expired_transaction_is_rejected(isolated_iam_database_url: str) -> None:
    app, container = _build_app(isolated_iam_database_url, FakeFederatedIdentityProvider())
    try:
        async with _client(app) as http_client:
            state = await _start(http_client)
            await _expire_transaction(isolated_iam_database_url, state)
            await asyncio.sleep(0.05)  # rebasa el `expires_at` recien fijado
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert response.headers["location"] == "/login?federated_error=expired"


async def test_google_error_denies_and_still_consumes_the_transaction(
    isolated_iam_database_url: str,
) -> None:
    app, container = _build_app(isolated_iam_database_url, FakeFederatedIdentityProvider())
    try:
        async with _client(app) as http_client:
            state = await _start(http_client)
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&error=access_denied", follow_redirects=False
            )
            replay = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&error=access_denied", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert response.headers["location"] == "/login?federated_error=denied"
    assert replay.headers["location"] == "/login?federated_error=expired"


@pytest.mark.parametrize(
    "query",
    [
        "",  # sin state
        "state=abc",  # sin code ni error
        "state=abc&code=x&error=access_denied",  # los dos a la vez
    ],
)
async def test_malformed_callback_requests_are_rejected_as_expired(
    isolated_iam_database_url: str, query: str
) -> None:
    app, container = _build_app(isolated_iam_database_url, FakeFederatedIdentityProvider())
    try:
        async with _client(app) as http_client:
            response = await http_client.get(
                f"{_CALLBACK_PATH}?{query}" if query else _CALLBACK_PATH,
                follow_redirects=False,
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert response.headers["location"] == "/login?federated_error=expired"


# ---------------------------------------------------------------------------
# Proveedor caido / respuesta inesperada
# ---------------------------------------------------------------------------


async def test_provider_failure_answers_provider_unavailable(
    isolated_iam_database_url: str,
) -> None:
    provider = FakeFederatedIdentityProvider()
    app, container = _build_app(isolated_iam_database_url, provider)
    try:
        async with _client(app) as http_client:
            state = await _start(http_client)
            provider.failure = RuntimeError("Google no responde")
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert response.headers["location"] == "/login?federated_error=provider_unavailable"
    assert await _count(isolated_iam_database_url, "owners") == 0


async def test_a_provider_failure_burns_the_state_too(isolated_iam_database_url: str) -> None:
    """Code review 17-sep (item 4, C-75 pieza 3): la referencia se consume
    en la fase (a), ANTES de llamar a Google -- un fallo posterior del
    proveedor no la resucita. Repetir el mismo `state` tras el fallo debe
    responder `expired`, nunca reintentar el canje ni volver a intentar
    `provider_unavailable`."""
    provider = FakeFederatedIdentityProvider()
    app, container = _build_app(isolated_iam_database_url, provider)
    try:
        async with _client(app) as http_client:
            state = await _start(http_client)
            provider.failure = RuntimeError("Google no responde")
            first = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
            replay = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert first.headers["location"] == "/login?federated_error=provider_unavailable"
    assert replay.status_code == 303
    assert replay.headers["location"] == "/login?federated_error=expired"
    assert await _count(isolated_iam_database_url, "owners") == 0


# ---------------------------------------------------------------------------
# T084 C-75 pieza 2: el canje contra Google no retiene una conexion
# ---------------------------------------------------------------------------


class _PoolProbingProvider:
    """Envuelve `FakeFederatedIdentityProvider` para leer el pool de
    conexiones del `Container` EN MEDIO del canje -- si `_run_callback_
    phases` mantuviera la sesion de la fase (a) abierta durante la llamada
    al proveedor (como antes de esta pieza), esto veria al menos una
    conexion prestada."""

    def __init__(self, inner: FakeFederatedIdentityProvider, container: Container) -> None:
        self._inner = inner
        self._container = container
        self.checked_out_during_exchange: int | None = None

    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        return self._inner.authorization_url(state=state, nonce=nonce, redirect_uri=redirect_uri)

    async def exchange_code(
        self, *, code: str, redirect_uri: str, expected_nonce_hash: ReferenceHash
    ) -> object:
        self.checked_out_during_exchange = self._container.engine.pool.checkedout()
        return await self._inner.exchange_code(
            code=code, redirect_uri=redirect_uri, expected_nonce_hash=expected_nonce_hash
        )


async def test_callback_does_not_hold_a_db_connection_during_the_provider_call(
    isolated_iam_database_url: str,
) -> None:
    settings = build_api_settings(
        database_url=isolated_iam_database_url, public_base_url=_PUBLIC_BASE_URL
    )
    container = Container.build(settings)
    probe = _PoolProbingProvider(FakeFederatedIdentityProvider(), container)
    fastapi_app = FastAPI()
    fastapi_app.state.container = container
    fastapi_app.add_exception_handler(ApiError, _handle_api_error)
    fastapi_app.include_router(
        build_federated_router(
            provider=probe,  # type: ignore[arg-type]
            redirect_uri=_REDIRECT_URI,
            allowed_emails=AuthorizedEmailList.from_raw((_ALLOWED_EMAIL,)),
            open_consent_transactions=_SqlOpenConsentTransactions(container),
            trusted_proxy_hops=settings.trusted_proxy_hops,
        )
    )
    try:
        async with _client(fastapi_app) as http_client:
            state = await _start(http_client)
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert probe.checked_out_during_exchange == 0


# ---------------------------------------------------------------------------
# El callback nunca refleja la entrada y siempre responde 303
# ---------------------------------------------------------------------------


async def test_callback_never_reflects_the_query_string(isolated_iam_database_url: str) -> None:
    app, container = _build_app(isolated_iam_database_url, FakeFederatedIdentityProvider())
    marker = "<script>evil</script>"
    try:
        async with _client(app) as http_client:
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state=never-issued&code={marker}", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert marker not in response.text
    assert marker not in response.headers["location"]
    assert response.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# T053 (US2): re-identificacion -- FR-113, checkpoints/us1.md
# ---------------------------------------------------------------------------

_TOTP_ENC_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _build_full_app(
    isolated_iam_database_url: str, provider: FakeFederatedIdentityProvider
) -> tuple[FastAPI, Container]:
    """`federated_router.py` + `consent_router.py` together: the US2 story
    spans re-identifying and then approving under the SAME freshness."""
    settings = build_api_settings(
        database_url=isolated_iam_database_url, public_base_url=_PUBLIC_BASE_URL
    )
    container = Container.build(settings)
    fastapi_app = FastAPI()
    fastapi_app.state.container = container
    fastapi_app.add_exception_handler(ApiError, _handle_api_error)
    fastapi_app.include_router(
        build_federated_router(
            provider=provider,
            redirect_uri=_REDIRECT_URI,
            allowed_emails=AuthorizedEmailList.from_raw((_ALLOWED_EMAIL,)),
            open_consent_transactions=_SqlOpenConsentTransactions(container),
            trusted_proxy_hops=settings.trusted_proxy_hops,
        )
    )
    fastapi_app.include_router(
        build_consent_router(
            token_hasher=Sha256TokenHasher(),
            token_factory=SecretsOpaqueTokenFactory(),
            totp_enc_key=_TOTP_ENC_KEY,
            public_base_url=_PUBLIC_BASE_URL,
            federated_available=True,
        )
    )
    return fastapi_app, container


def _client_with_session(app: FastAPI, *, raw_token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
        cookies={SESSION_COOKIE_NAME: raw_token},
    )


async def _seed_federated_owner(
    isolated_iam_database_url: str,
    *,
    last_federated_auth_at: datetime,
    subject: str,
    created_at: datetime | None = None,
    email: str = _ALLOWED_EMAIL,
) -> tuple[UUID, str]:
    """Owner with a bound federated identity and NO TOTP (FR-109: born from
    Google, no other way in) -- `require_fresh_identification` only ever
    offers `["federated"]` for it. `created_at` lets a test control which
    of several seeded owners the sole-owner TOFU resolves to
    (`SELECT ... ORDER BY created_at ASC LIMIT 1`)."""
    owner_id = uuid4()
    raw_token = f"reidentify-test-token-{uuid4().hex}"
    owner_created_at = created_at or datetime.now(UTC)
    session_created_at = datetime.now(UTC)
    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO owners (id, email, password_hash, created_at) "
                "VALUES (:id, :email, :password_hash, :created_at)"
            ),
            {
                "id": str(owner_id),
                "email": email,
                "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
                "created_at": owner_created_at,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO sessions (id, owner_id, token_hash, created_at, expires_at, "
                "origin, last_federated_auth_at) "
                "VALUES (:id, :owner_id, :token_hash, :created_at, :expires_at, "
                "'federated', :last_federated_auth_at)"
            ),
            {
                "id": str(uuid4()),
                "owner_id": str(owner_id),
                "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                "created_at": session_created_at,
                "expires_at": session_created_at + timedelta(hours=1),
                "last_federated_auth_at": last_federated_auth_at,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO owner_federated_identities (owner_id, issuer, subject, "
                "email_at_binding, bound_at, last_seen_at) VALUES "
                "(:owner_id, 'https://accounts.google.com', :subject, :email, :now, :now)"
            ),
            {
                "owner_id": str(owner_id),
                "subject": subject,
                "email": email,
                "now": session_created_at,
            },
        )
    await engine.dispose()
    return owner_id, raw_token


async def _revoke_session(isolated_iam_database_url: str, *, raw_token: str) -> None:
    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE sessions SET revoked_at = :now WHERE token_hash = :token_hash"),
            {
                "now": datetime.now(UTC),
                "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            },
        )
    await engine.dispose()


async def _last_federated_auth_at(isolated_iam_database_url: str, *, owner_id: UUID) -> datetime:
    engine = create_async_engine(isolated_iam_database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        row = (
            (
                await connection.execute(
                    text("SELECT last_federated_auth_at FROM sessions WHERE owner_id = :id"),
                    {"id": str(owner_id)},
                )
            )
            .mappings()
            .one()
        )
    await engine.dispose()
    return row["last_federated_auth_at"]


async def test_reidentify_completes_the_pending_transaction_and_freshens_a_second_one_too(
    isolated_iam_database_url: str,
) -> None:
    stale_at = datetime.now(UTC) - timedelta(minutes=10)
    subject = "google-sub-reidentify-a"
    owner_id, raw_token = await _seed_federated_owner(
        isolated_iam_database_url, last_federated_auth_at=stale_at, subject=subject
    )
    txn_id = await _seed_pending_authorization_request(isolated_iam_database_url)
    provider = FakeFederatedIdentityProvider(
        subject=FederatedSubject(subject), email=Email(_ALLOWED_EMAIL)
    )
    app, container = _build_full_app(isolated_iam_database_url, provider)
    try:
        async with _client_with_session(app, raw_token=raw_token) as http_client:
            stale_response = await http_client.post(f"/api/v1/mcp-oauth/consent/{txn_id}/approve")
            assert stale_response.status_code == 401
            assert stale_response.json()["error"]["details"]["methods"] == ["federated"]

            state = await _start(http_client, txn_id=txn_id)
            callback_response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
            assert callback_response.status_code == 303
            assert callback_response.headers["location"] == f"/oauth/autorizar?txn={txn_id}"

            still_pending = await http_client.get(f"/api/v1/mcp-oauth/consent/{txn_id}")
            assert still_pending.status_code == 200

            approved = await http_client.post(f"/api/v1/mcp-oauth/consent/{txn_id}/approve")
            assert approved.status_code == 200, approved.text

            second_txn_id = await _seed_pending_authorization_request(isolated_iam_database_url)
            second_approved = await http_client.post(
                f"/api/v1/mcp-oauth/consent/{second_txn_id}/approve"
            )
            assert second_approved.status_code == 200, second_approved.text
    finally:
        await container.aclose()

    assert await _count(isolated_iam_database_url, "owners") == 1


async def test_reidentify_rejected_when_the_session_that_opened_it_is_no_longer_active(
    isolated_iam_database_url: str,
) -> None:
    """`state` de otra sesion (tasks.md T053): la que abrio el salto ya no
    esta activa por el momento de la vuelta -- rechazado sin marcar
    frescura, y el fallo vuelve a la MISMA pantalla de consentimiento
    (checkpoints/us1.md), nunca a `/login`."""
    stale_at = datetime.now(UTC) - timedelta(minutes=10)
    subject = "google-sub-reidentify-b"
    owner_id, raw_token = await _seed_federated_owner(
        isolated_iam_database_url, last_federated_auth_at=stale_at, subject=subject
    )
    txn_id = await _seed_pending_authorization_request(isolated_iam_database_url)
    provider = FakeFederatedIdentityProvider(
        subject=FederatedSubject(subject), email=Email(_ALLOWED_EMAIL)
    )
    app, container = _build_full_app(isolated_iam_database_url, provider)
    try:
        async with _client_with_session(app, raw_token=raw_token) as http_client:
            state = await _start(http_client, txn_id=txn_id)
            await _revoke_session(isolated_iam_database_url, raw_token=raw_token)
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert response.headers["location"] == f"/oauth/autorizar?txn={txn_id}&federated_error=expired"

    unchanged = await _last_federated_auth_at(isolated_iam_database_url, owner_id=owner_id)
    assert abs((unchanged - stale_at).total_seconds()) < 1


async def test_identity_mismatch_during_reidentify_returns_to_consent_screen_without_marking(
    isolated_iam_database_url: str,
) -> None:
    """Cuenta de Google que resuelve a otro dueno (tasks.md T053): el modelo
    es de un unico dueno por despliegue (TOFU por `created_at` mas
    antiguo) -- una sesion que no pertenece a ESE dueno nunca puede
    refrescarse a si misma, sea cual sea la cuenta de Google."""
    now = datetime.now(UTC)
    sole_subject = "google-sub-sole-owner"
    await _seed_federated_owner(
        isolated_iam_database_url,
        created_at=now - timedelta(hours=1),
        last_federated_auth_at=now - timedelta(hours=1),
        subject=sole_subject,
    )
    stale_at = now - timedelta(minutes=10)
    other_owner_id, other_raw_token = await _seed_federated_owner(
        isolated_iam_database_url,
        created_at=now,
        last_federated_auth_at=stale_at,
        subject="google-sub-other-owner",
        email="otro@example.com",
    )
    txn_id = await _seed_pending_authorization_request(isolated_iam_database_url)
    provider = FakeFederatedIdentityProvider(
        subject=FederatedSubject(sole_subject), email=Email(_ALLOWED_EMAIL)
    )
    app, container = _build_full_app(isolated_iam_database_url, provider)
    try:
        async with _client_with_session(app, raw_token=other_raw_token) as http_client:
            state = await _start(http_client, txn_id=txn_id)
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == f"/oauth/autorizar?txn={txn_id}&federated_error=identity_mismatch"
    )

    unchanged = await _last_federated_auth_at(isolated_iam_database_url, owner_id=other_owner_id)
    assert abs((unchanged - stale_at).total_seconds()) < 1


async def test_provider_down_during_reidentify_returns_to_consent_screen_not_login(
    isolated_iam_database_url: str,
) -> None:
    """checkpoints/us1.md, abierto: CUALQUIER fallo del tramo federado con
    proposito `reidentify` vuelve a la misma transaccion -- no solo
    `identity_mismatch`. Aqui el proveedor se cae a mitad del canje."""
    stale_at = datetime.now(UTC) - timedelta(minutes=10)
    subject = "google-sub-reidentify-c"
    _owner_id, raw_token = await _seed_federated_owner(
        isolated_iam_database_url, last_federated_auth_at=stale_at, subject=subject
    )
    txn_id = await _seed_pending_authorization_request(isolated_iam_database_url)
    provider = FakeFederatedIdentityProvider(
        subject=FederatedSubject(subject), email=Email(_ALLOWED_EMAIL)
    )
    app, container = _build_full_app(isolated_iam_database_url, provider)
    try:
        async with _client_with_session(app, raw_token=raw_token) as http_client:
            state = await _start(http_client, txn_id=txn_id)
            provider.failure = RuntimeError("Google no responde")
            response = await http_client.get(
                f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
            )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert (
        response.headers["location"]
        == f"/oauth/autorizar?txn={txn_id}&federated_error=provider_unavailable"
    )


# ---------------------------------------------------------------------------
# T080: eventos estructurados sin credenciales (NFR-106/SC-107)
# ---------------------------------------------------------------------------


async def test_successful_login_logs_an_outcome_without_state_or_code(
    isolated_iam_database_url: str,
) -> None:
    """`client_secret`: el propio doble (`FakeFederatedIdentityProvider`)
    no lo usa -- esa higiene ya la cubre `tests/unit/composition/
    test_federated_routes_gating.py::test_client_secret_never_appears_in_any_log_line`
    a nivel de registro/arranque. Aqui lo que se ejercita es NUEVO:
    `federated_router.py`/`resolve_federated_login.py` logueando una
    vuelta REAL con un `state`/`code` conocidos."""
    provider = FakeFederatedIdentityProvider()
    app, container = _build_app(isolated_iam_database_url, provider)
    marker_code = "super-secret-authorization-code-marker"
    try:
        with structlog.testing.capture_logs() as logs:
            async with _client(app) as http_client:
                state = await _start(http_client)
                response = await http_client.get(
                    f"{_CALLBACK_PATH}?state={state}&code={marker_code}",
                    follow_redirects=False,
                )
    finally:
        await container.aclose()

    assert response.status_code == 303
    assert any(entry["event"] == "federated_login_attempt_succeeded" for entry in logs)
    for entry in logs:
        rendered = repr(entry)
        assert state not in rendered
        assert marker_code not in rendered


async def test_denied_login_logs_a_reason_code_without_the_claimed_email(
    isolated_iam_database_url: str,
) -> None:
    claimed_email = "intruso@example.com"
    provider = FakeFederatedIdentityProvider(email=Email(claimed_email))
    app, container = _build_app(isolated_iam_database_url, provider)
    try:
        with structlog.testing.capture_logs() as logs:
            async with _client(app) as http_client:
                state = await _start(http_client)
                response = await http_client.get(
                    f"{_CALLBACK_PATH}?state={state}&code=fake-code", follow_redirects=False
                )
    finally:
        await container.aclose()

    assert response.status_code == 303
    attempt = next(entry for entry in logs if entry["event"] == "federated_login_attempt_failed")
    assert attempt["outcome"] == "denied"
    assert attempt["reason"] == "email_not_allowed"
    for entry in logs:
        assert claimed_email not in repr(entry)
