"""`GET /api/v1/auth/me` (spec 002b tasks.md T050, contracts/federated-login.md
§2) against real Postgres: the three session origins (`federated`/
`password`/`bridge`), the derived `fresh_identification_until`, and the
switch off collapsing both new fields (`federated_login_available: false`,
`fresh_identification_until: null`) regardless of what the row says.

Router mount isolated (FastAPI empty), same pattern as
`tests/integration/mcp_oauth/test_consent_router.py`."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.application.session_policy import FEDERATED_IDENTIFICATION_TTL
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.iam.presentation.router import build_auth_router

pytestmark = pytest.mark.integration

_ALLOWED_EMAIL = "dueno@example.com"


class _SeededSession:
    def __init__(self, *, owner_id: uuid.UUID, raw_token: str) -> None:
        self.owner_id = owner_id
        self.raw_token = raw_token


async def _seed_owner_with_session(
    session: AsyncSession, *, origin: str, last_federated_auth_at: datetime | None
) -> _SeededSession:
    owner_id = uuid.uuid4()
    raw_token = f"auth-me-test-token-{uuid.uuid4().hex}"
    now = datetime.now(UTC)
    await session.execute(
        text("INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, :password_hash)"),
        {
            "id": str(owner_id),
            "email": f"owner-{owner_id.hex[:8]}@example.com",
            "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
        },
    )
    await session.execute(
        text(
            "INSERT INTO sessions (id, owner_id, token_hash, created_at, expires_at, "
            "origin, last_federated_auth_at) "
            "VALUES (:id, :owner_id, :token_hash, :created_at, :expires_at, "
            ":origin, :last_federated_auth_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "owner_id": str(owner_id),
            "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            "created_at": now,
            "expires_at": now + timedelta(hours=1),
            "origin": origin,
            "last_federated_auth_at": last_federated_auth_at,
        },
    )
    return _SeededSession(owner_id=owner_id, raw_token=raw_token)


def _build_app(settings: ApiSettings) -> tuple[FastAPI, Container]:
    container = Container.build(settings)
    fastapi_app = FastAPI()
    fastapi_app.state.container = container
    fastapi_app.add_exception_handler(ApiError, _handle_api_error)
    fastapi_app.include_router(build_auth_router(settings))
    return fastapi_app, container


def _client_for(app: FastAPI, *, raw_token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
        cookies={SESSION_COOKIE_NAME: raw_token},
    )


_FEDERATED_SETTINGS_OVERRIDES = {
    "federated_login_enabled": True,
    "google_oidc_client_id": "federated-client-id",
    "google_oidc_client_secret": "federated-client-secret",
    "federated_allowed_emails": [_ALLOWED_EMAIL],
}


async def _seed(
    database_url: str, *, origin: str, last_federated_auth_at: datetime | None
) -> _SeededSession:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        seeded = await _seed_owner_with_session(
            session, origin=origin, last_federated_auth_at=last_federated_auth_at
        )
        await session.commit()
    await engine.dispose()
    return seeded


async def _cleanup(database_url: str, owner_id: uuid.UUID) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(owner_id)}
        )
        await connection.execute(text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)})
    await engine.dispose()


async def test_federated_session_reports_origin_and_derived_freshness(
    database_url: str,
) -> None:
    federated_at = datetime.now(UTC)
    seeded = await _seed(database_url, origin="federated", last_federated_auth_at=federated_at)
    settings = build_api_settings(database_url=database_url, **_FEDERATED_SETTINGS_OVERRIDES)
    app, container = _build_app(settings)
    try:
        async with _client_for(app, raw_token=seeded.raw_token) as http_client:
            response = await http_client.get("/api/v1/auth/me")
    finally:
        await container.aclose()
        await _cleanup(database_url, seeded.owner_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["origin"] == "federated"
    assert body["federated_login_available"] is True
    expected = federated_at + FEDERATED_IDENTIFICATION_TTL
    actual = datetime.fromisoformat(body["session"]["fresh_identification_until"])
    assert abs((actual - expected).total_seconds()) < 1
    assert response.headers["cache-control"] == "no-store"


async def test_password_session_has_no_freshness_hint(database_url: str) -> None:
    seeded = await _seed(database_url, origin="password", last_federated_auth_at=None)
    settings = build_api_settings(database_url=database_url, **_FEDERATED_SETTINGS_OVERRIDES)
    app, container = _build_app(settings)
    try:
        async with _client_for(app, raw_token=seeded.raw_token) as http_client:
            response = await http_client.get("/api/v1/auth/me")
    finally:
        await container.aclose()
        await _cleanup(database_url, seeded.owner_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["origin"] == "password"
    assert body["session"]["fresh_identification_until"] is None
    assert body["federated_login_available"] is True


async def test_bridge_session_reports_its_origin(database_url: str) -> None:
    seeded = await _seed(database_url, origin="bridge", last_federated_auth_at=None)
    settings = build_api_settings(database_url=database_url, **_FEDERATED_SETTINGS_OVERRIDES)
    app, container = _build_app(settings)
    try:
        async with _client_for(app, raw_token=seeded.raw_token) as http_client:
            response = await http_client.get("/api/v1/auth/me")
    finally:
        await container.aclose()
        await _cleanup(database_url, seeded.owner_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["origin"] == "bridge"
    assert body["session"]["fresh_identification_until"] is None


async def test_password_session_refreshed_with_google_still_gets_a_freshness_hint(
    database_url: str,
) -> None:
    """Decision 7 (research.md): a password-born session CAN be refreshed
    via a Google re-identification; `origin` is never rewritten, but the
    freshness hint is still derived from `last_federated_auth_at`."""
    federated_at = datetime.now(UTC)
    seeded = await _seed(database_url, origin="password", last_federated_auth_at=federated_at)
    settings = build_api_settings(database_url=database_url, **_FEDERATED_SETTINGS_OVERRIDES)
    app, container = _build_app(settings)
    try:
        async with _client_for(app, raw_token=seeded.raw_token) as http_client:
            response = await http_client.get("/api/v1/auth/me")
    finally:
        await container.aclose()
        await _cleanup(database_url, seeded.owner_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["origin"] == "password"
    assert body["session"]["fresh_identification_until"] is not None


async def test_switch_off_hides_the_freshness_hint_even_with_a_federated_session(
    database_url: str,
) -> None:
    federated_at = datetime.now(UTC)
    seeded = await _seed(database_url, origin="federated", last_federated_auth_at=federated_at)
    settings = build_api_settings(database_url=database_url)
    app, container = _build_app(settings)
    try:
        async with _client_for(app, raw_token=seeded.raw_token) as http_client:
            response = await http_client.get("/api/v1/auth/me")
    finally:
        await container.aclose()
        await _cleanup(database_url, seeded.owner_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["origin"] == "federated"
    assert body["session"]["fresh_identification_until"] is None
    assert body["federated_login_available"] is False


async def test_a_c82_invalidated_federated_mark_never_leaks_a_fabricated_date(
    database_url: str,
) -> None:
    """T064 security review, re-verificación de C-82: una sesión
    `origin='federated'` cuya marca fue invalidada por una revocación
    (`grants_router.py`) queda con un instante MUY antiguo, nunca `NULL`
    (`sessions_federated_origin_check` lo prohíbe). `fresh_identification_
    until` no debe sumarle la ventana a esa fecha fabricada -- debe ser
    `null`, exactamente como una marca ausente."""
    invalidated_mark = datetime(2016, 1, 1, tzinfo=UTC)
    seeded = await _seed(database_url, origin="federated", last_federated_auth_at=invalidated_mark)
    settings = build_api_settings(database_url=database_url, **_FEDERATED_SETTINGS_OVERRIDES)
    app, container = _build_app(settings)
    try:
        async with _client_for(app, raw_token=seeded.raw_token) as http_client:
            response = await http_client.get("/api/v1/auth/me")
    finally:
        await container.aclose()
        await _cleanup(database_url, seeded.owner_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session"]["fresh_identification_until"] is None
