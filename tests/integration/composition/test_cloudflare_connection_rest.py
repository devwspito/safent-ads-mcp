"""`/api/v1/integrations/cloudflare*` de extremo a extremo contra Postgres
real (lane 006-cloudflare-ui, owner decision): `GET` refleja la fila
guardada (o el respaldo de entorno, o desconectado), `DELETE` la borra, y
el cuerpo de `POST` se valida ANTES de tocar la red -- mismo patron de
sesion real + `httpx.ASGITransport` que `test_telegram_pairing_rest.py`.
Ningun test de este fichero ejercita un `POST` que llegue a validar contra
la Cloudflare real: eso lo cubre `test_connection_service.py` con un
`client_factory` inyectado sobre `httpx.MockTransport`."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import _handle_api_error, _handle_validation_error
from safent_ads.composition.container import Container
from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_CLOUDFLARE_API_TOKEN,
    AesGcmTotpCipher,
)
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.integrations.cloudflare.rest import build_cloudflare_connection_router

pytestmark = pytest.mark.integration

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_ACCOUNT_ID = "a" * 32
_TOKEN = "sk-cloudflare-super-secret-token-do-not-leak"  # noqa: S105 - fixture


class _SeededOwner:
    def __init__(self, *, owner_id: uuid.UUID, raw_token: str, email: str) -> None:
        self.owner_id = owner_id
        self.raw_token = raw_token
        self.email = email


async def _seed_owner(session: AsyncSession) -> _SeededOwner:
    owner_id = uuid.uuid4()
    raw_token = f"cloudflare-rest-test-token-{uuid.uuid4().hex}"
    email = f"owner-{owner_id.hex[:8]}@safent.example"
    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO owners (id, email, password_hash) "
            "VALUES (:id, :email, :password_hash)"
        ),
        {
            "id": str(owner_id),
            "email": email,
            "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
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
    return _SeededOwner(owner_id=owner_id, raw_token=raw_token, email=email)


@pytest.fixture
async def seeded_owner(database_url: str) -> AsyncIterator[_SeededOwner]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        owner = await _seed_owner(session)
        await session.commit()
    try:
        yield owner
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM cloudflare_connection"))
            await connection.execute(
                text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(owner.owner_id)}
            )
        await engine.dispose()


def _app(container: Container, *, fallback_token_configured: bool = False) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.include_router(
        build_cloudflare_connection_router(
            session_factory=container.session_factory,
            totp_enc_key=_VALID_32_BYTE_KEY_B64,
            fallback_token_configured=fallback_token_configured,
            clock=container.clock,
        )
    )
    return app


def _client(app: FastAPI, *, raw_token: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies={SESSION_COOKIE_NAME: raw_token, "ads_csrf": "test-csrf"},
        headers={"X-CSRF-Token": "test-csrf"},
    )


async def _seed_connection(container: Container, *, owner_id: uuid.UUID) -> None:
    async with container.session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO cloudflare_connection "
                "(id, api_token_encrypted, account_id, zones, "
                "connected_at, connected_by_owner_id) "
                "VALUES (TRUE, :token, :account_id, CAST(:zones AS JSONB), "
                ":connected_at, :owner_id)"
            ),
            {
                "token": AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64).encrypt(
                    _TOKEN, purpose=PURPOSE_CLOUDFLARE_API_TOKEN
                ),
                "account_id": _ACCOUNT_ID,
                "zones": '["example.com"]',
                "connected_at": datetime(2026, 9, 15, tzinfo=UTC),
                "owner_id": str(owner_id),
            },
        )
        await session.commit()


async def test_get_reports_disconnected_by_default(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        async with _client(_app(container), raw_token=seeded_owner.raw_token) as client:
            response = await client.get("/api/v1/integrations/cloudflare")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["connected"] is False
        assert body["zones"] == []
        expected_url = "https://dash.cloudflare.com/profile/api-tokens"  # noqa: S105
        assert body["create_token_url"] == expected_url
        assert body["required_permissions"] == ["Zone.Read", "DNS.Edit"]
    finally:
        await container.aclose()


async def test_get_reports_connected_when_the_env_fallback_is_set(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        app = _app(container, fallback_token_configured=True)
        async with _client(app, raw_token=seeded_owner.raw_token) as client:
            response = await client.get("/api/v1/integrations/cloudflare")
        assert response.json()["connected"] is True
    finally:
        await container.aclose()


async def test_get_reflects_a_stored_connection_and_never_echoes_the_token(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        await _seed_connection(container, owner_id=seeded_owner.owner_id)
        async with _client(_app(container), raw_token=seeded_owner.raw_token) as client:
            response = await client.get("/api/v1/integrations/cloudflare")
        body = response.json()
        assert body["connected"] is True
        assert body["account_id"] == _ACCOUNT_ID
        assert body["zones"] == ["example.com"]
        assert body["create_token_url"] == f"https://dash.cloudflare.com/{_ACCOUNT_ID}/api-tokens"
        assert _TOKEN not in response.text
    finally:
        await container.aclose()


async def test_delete_removes_the_stored_connection(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        await _seed_connection(container, owner_id=seeded_owner.owner_id)
        async with _client(_app(container), raw_token=seeded_owner.raw_token) as client:
            delete_response = await client.request(
                "DELETE",
                "/api/v1/integrations/cloudflare/token",
                json={"typed_confirmation": "DESCONECTAR"},
            )
            get_response = await client.get("/api/v1/integrations/cloudflare")
        assert delete_response.status_code == 204
        assert get_response.json()["connected"] is False
    finally:
        await container.aclose()


async def test_delete_records_a_decision_log_entry_that_survives_the_row(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    """Hallazgo medio (revision de seguridad 2026-09-15): `DELETE` borraba
    la fila sin dejar rastro -- `decision_log` debe conservar
    `account_id`/`zone_count`/`connected_by_owner_id` aunque la fila de
    `cloudflare_connection` ya no exista."""
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        await _seed_connection(container, owner_id=seeded_owner.owner_id)
        async with _client(_app(container), raw_token=seeded_owner.raw_token) as client:
            delete_response = await client.request(
                "DELETE",
                "/api/v1/integrations/cloudflare/token",
                json={"typed_confirmation": "DESCONECTAR"},
            )
        assert delete_response.status_code == 204

        async with container.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT actor_id, payload::text AS payload_text FROM decision_log "
                        "WHERE event_type = 'cloudflare_disconnected' "
                        "ORDER BY seq DESC LIMIT 1"
                    )
                )
            ).mappings().first()
        assert row is not None
        assert row["actor_id"] == seeded_owner.email
        assert json.loads(row["payload_text"]) == {
            "account_id": _ACCOUNT_ID,
            "zone_count": 1,
            "connected_by_owner_id": str(seeded_owner.owner_id),
        }
    finally:
        await container.aclose()


async def test_delete_requires_the_typed_phrase(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        await _seed_connection(container, owner_id=seeded_owner.owner_id)
        async with _client(_app(container), raw_token=seeded_owner.raw_token) as client:
            response = await client.request(
                "DELETE", "/api/v1/integrations/cloudflare/token", json={"typed_confirmation": "no"}
            )
            get_response = await client.get("/api/v1/integrations/cloudflare")
        assert response.status_code == 428
        assert get_response.json()["connected"] is True
    finally:
        await container.aclose()


async def test_post_without_a_session_is_rejected_before_touching_cloudflare(
    database_url: str,
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        async with _client(_app(container), raw_token="not-a-real-session-token") as client:
            response = await client.post(
                "/api/v1/integrations/cloudflare/token", json={"token": _TOKEN}
            )
        # Sin sesion de propietario resuelta (`CURRENT_OWNER`) la peticion
        # nunca llega a `ConnectCloudflareToken.execute` -- 401, nunca un
        # intento de red hacia Cloudflare con un token de prueba.
        assert response.status_code == 401
    finally:
        await container.aclose()


async def test_post_rejects_a_malformed_account_id_before_touching_cloudflare(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        async with _client(_app(container), raw_token=seeded_owner.raw_token) as client:
            response = await client.post(
                "/api/v1/integrations/cloudflare/token",
                json={"token": _TOKEN, "account_id": "not-a-valid-account-id"},
            )
        assert response.status_code == 422
    finally:
        await container.aclose()


async def test_post_with_a_malformed_account_id_never_echoes_the_submitted_value(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    """Hallazgo bajo (revision de seguridad 2026-09-15): el manejador por
    defecto de FastAPI para `RequestValidationError` devuelve `input` --
    un token pegado por error en `account_id` se veia reflejado en el
    cuerpo del 422."""
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    pasted_token = "sk-cloudflare-real-secret-pasted-by-mistake"  # noqa: S105 - fixture
    try:
        async with _client(_app(container), raw_token=seeded_owner.raw_token) as client:
            response = await client.post(
                "/api/v1/integrations/cloudflare/token",
                json={"token": _TOKEN, "account_id": pasted_token},
            )
        assert response.status_code == 422
        assert pasted_token not in response.text
        body = response.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert body["error"]["details"]["errors"] == [
            {"field": "body.account_id", "message": "String should match pattern '^[0-9a-f]{32}$'"}
        ]
    finally:
        await container.aclose()
