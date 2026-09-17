"""`/api/v1/telegram/pairing*` de extremo a extremo contra Postgres real
(rest-api.md §Conexiones, Telegram y ajustes; contracts/telegram.md
§Emparejamiento; FR-25): las 4 rutas, sus 409, el `X-Action-Confirmation` de
`start` y el aislamiento por PROPIETARIO -- mismo patron de sesion real +
`httpx.ASGITransport` que `test_execution_rest_rules_and_batch.py`."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pyotp
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.integration.iam.confirmation_helpers import confirmed_request
from tests.unit.composition.factories import build_api_settings

from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_TOTP_SECRET,
    AesGcmTotpCipher,
)
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.notifications.infrastructure.telegram_pairing_sql import (
    SqlTelegramPairingConfirmation,
)
from safent_ads.notifications.presentation.rest import build_telegram_pairing_router

pytestmark = pytest.mark.integration

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_TOTP_SECRET = pyotp.random_base32()


def _fresh_chat_id() -> int:
    """`ix_telegram_owner_chats_paired_chat` (0021) es UNIQUE de verdad:
    cada test que empareja necesita su propio `chat_id`, nunca una
    constante compartida entre tests contra el mismo Postgres."""
    return 100_000_000 + (uuid.uuid4().int % 800_000_000)


class _SeededOwner:
    def __init__(self, *, owner_id: uuid.UUID, raw_token: str, business_id: uuid.UUID) -> None:
        self.owner_id = owner_id
        self.raw_token = raw_token
        self.business_id = business_id


class _TwoOwners:
    def __init__(self, *, owner_a: _SeededOwner, owner_b: _SeededOwner) -> None:
        self.owner_a = owner_a
        self.owner_b = owner_b


async def _seed_owner(
    session: AsyncSession, *, with_totp: bool, business_id: uuid.UUID
) -> _SeededOwner:
    owner_id = uuid.uuid4()
    raw_token = f"pairing-test-token-{uuid.uuid4().hex}"
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
    return _SeededOwner(owner_id=owner_id, raw_token=raw_token, business_id=business_id)


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
async def two_owners(database_url: str) -> AsyncIterator[_TwoOwners]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        business_id = await _seed_business(session)
        owner_a = await _seed_owner(session, with_totp=True, business_id=business_id)
        owner_b = await _seed_owner(session, with_totp=False, business_id=business_id)
        await session.commit()
    try:
        yield _TwoOwners(owner_a=owner_a, owner_b=owner_b)
    finally:
        # No se borran `owners`: `telegram_owner_chats`/`telegram_test_messages`
        # referencian `owner_id` con `ON DELETE RESTRICT` (0021) -- mismo
        # criterio que `test_idor_sweep_new_routes.py::two_businesses`
        # (solo limpia `sessions`, deja el resto de filas de fixture).
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM sessions WHERE owner_id = ANY(:ids)"),
                {"ids": [str(owner_a.owner_id), str(owner_b.owner_id)]},
            )
        await engine.dispose()


def _app(container: Container, *, allowlist_configured: bool = True) -> FastAPI:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(
        build_telegram_pairing_router(
            session_factory=container.session_factory,
            totp_enc_key=_VALID_32_BYTE_KEY_B64,
            allowlist_configured=allowlist_configured,
            clock=container.clock,
            id_generator=container.id_generator,
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


async def _confirm_pairing(container: Container, *, owner_id: uuid.UUID, chat_id: int) -> None:
    """Simula `/emparejar <codigo>` resuelto por el bot: mismo camino que
    `TelegramPairingCommandResolver`, sin pasar por aiogram."""
    async with container.session_factory() as session:
        await SqlTelegramPairingConfirmation(session).confirm(
            owner_id=owner_id, chat_id=chat_id, at=container.clock.now()
        )
        await session.commit()


async def test_get_pairing_defaults_to_unpaired(two_owners: _TwoOwners, database_url: str) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        async with _client(_app(container), raw_token=two_owners.owner_a.raw_token) as client:
            response = await client.get(
                "/api/v1/telegram/pairing",
                params={"business_id": str(two_owners.owner_a.business_id)},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "unpaired"
        assert body["chat_id_masked"] is None
        assert body["pairing_code"] is None
        assert body["allowlist_configured"] is True
    finally:
        await container.aclose()


async def test_start_pairing_without_allowlist_is_409(
    two_owners: _TwoOwners, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        app = _app(container, allowlist_configured=False)
        async with _client(app, raw_token=two_owners.owner_a.raw_token) as client:
            response = await confirmed_request(client, "POST", "/api/v1/telegram/pairing/start")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TELEGRAM_ALLOWLIST_EMPTY"
    finally:
        await container.aclose()


async def test_start_pairing_requires_explicit_confirmation(
    two_owners: _TwoOwners, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        async with _client(_app(container), raw_token=two_owners.owner_a.raw_token) as client:
            response = await client.post("/api/v1/telegram/pairing/start")
        assert response.status_code == 428
        assert response.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    finally:
        await container.aclose()


async def test_start_pairing_issues_a_code_the_panel_can_poll_back(
    two_owners: _TwoOwners, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        app = _app(container)
        async with _client(app, raw_token=two_owners.owner_a.raw_token) as client:
            start_response = await confirmed_request(
                client, "POST", "/api/v1/telegram/pairing/start"
            )
            assert start_response.status_code == 201, start_response.text
            issued_code = start_response.json()["pairing_code"]
            assert len(issued_code) == 8

            get_response = await client.get(
                "/api/v1/telegram/pairing",
                params={"business_id": str(two_owners.owner_a.business_id)},
            )
        body = get_response.json()
        assert body["status"] == "pending"
        assert body["pairing_code"] == issued_code  # sobrevive un refresco de pagina
    finally:
        await container.aclose()


async def test_paired_owner_sees_masked_chat_id(two_owners: _TwoOwners, database_url: str) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    chat_id = _fresh_chat_id()
    try:
        app = _app(container)
        async with _client(app, raw_token=two_owners.owner_a.raw_token) as client:
            await confirmed_request(client, "POST", "/api/v1/telegram/pairing/start")
            await _confirm_pairing(container, owner_id=two_owners.owner_a.owner_id, chat_id=chat_id)

            response = await client.get(
                "/api/v1/telegram/pairing",
                params={"business_id": str(two_owners.owner_a.business_id)},
            )
        body = response.json()
        assert body["status"] == "paired"
        assert body["chat_id_masked"] == f"***{str(chat_id)[-4:]}"
        assert body["paired_at"] is not None
        assert body["pairing_code"] is None
    finally:
        await container.aclose()


async def test_test_message_without_pairing_is_409(
    two_owners: _TwoOwners, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        async with _client(_app(container), raw_token=two_owners.owner_a.raw_token) as client:
            response = await client.post("/api/v1/telegram/pairing/test-message")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "TELEGRAM_NOT_PAIRED"
    finally:
        await container.aclose()


async def test_test_message_enqueues_for_the_paired_chat(
    two_owners: _TwoOwners, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    chat_id = _fresh_chat_id()
    try:
        app = _app(container)
        async with _client(app, raw_token=two_owners.owner_a.raw_token) as client:
            await confirmed_request(client, "POST", "/api/v1/telegram/pairing/start")
            await _confirm_pairing(container, owner_id=two_owners.owner_a.owner_id, chat_id=chat_id)

            response = await client.post("/api/v1/telegram/pairing/test-message")
        assert response.status_code == 202, response.text
        notification_id = response.json()["notification_id"]

        async with container.session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT chat_id, delivery_state FROM telegram_test_messages "
                            "WHERE id = :id"
                        ),
                        {"id": notification_id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["chat_id"] == chat_id
        assert row["delivery_state"] == "PENDING"
    finally:
        await container.aclose()


async def test_a_fourth_test_message_within_ten_minutes_is_rate_limited(
    two_owners: _TwoOwners, database_url: str
) -> None:
    """security-review-f4.md item 1: sin freno, `POST
    /telegram/pairing/test-message` podia repetirse sin limite."""
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    chat_id = _fresh_chat_id()
    try:
        app = _app(container)
        async with _client(app, raw_token=two_owners.owner_a.raw_token) as client:
            await confirmed_request(client, "POST", "/api/v1/telegram/pairing/start")
            await _confirm_pairing(container, owner_id=two_owners.owner_a.owner_id, chat_id=chat_id)

            responses = [
                await client.post("/api/v1/telegram/pairing/test-message") for _ in range(3)
            ]
            fourth = await client.post("/api/v1/telegram/pairing/test-message")

        assert [response.status_code for response in responses] == [202, 202, 202]
        assert fourth.status_code == 429, fourth.text
        assert fourth.json()["error"]["code"] == "RATE_LIMITED"

        async with container.session_factory() as session:
            count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM telegram_test_messages WHERE chat_id = :chat_id"),
                    {"chat_id": chat_id},
                )
            ).scalar_one()
        assert count == 3
    finally:
        await container.aclose()


async def test_delete_pairing_requires_the_typed_phrase(
    two_owners: _TwoOwners, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        async with _client(_app(container), raw_token=two_owners.owner_a.raw_token) as client:
            response = await client.request(
                "DELETE", "/api/v1/telegram/pairing", json={"typed_confirmation": "algo"}
            )
        assert response.status_code == 428
        assert response.json()["error"]["code"] == "TYPED_CONFIRMATION_REQUIRED"
    finally:
        await container.aclose()


async def test_delete_pairing_unpairs_and_logs_the_decision(
    two_owners: _TwoOwners, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        app = _app(container)
        async with _client(app, raw_token=two_owners.owner_a.raw_token) as client:
            await confirmed_request(client, "POST", "/api/v1/telegram/pairing/start")
            await _confirm_pairing(
                container, owner_id=two_owners.owner_a.owner_id, chat_id=_fresh_chat_id()
            )

            delete_response = await client.request(
                "DELETE", "/api/v1/telegram/pairing", json={"typed_confirmation": "desemparejar"}
            )
            assert delete_response.status_code == 204, delete_response.text

            get_response = await client.get(
                "/api/v1/telegram/pairing",
                params={"business_id": str(two_owners.owner_a.business_id)},
            )
        assert get_response.json()["status"] == "unpaired"

        async with container.session_factory() as session:
            logged = (
                await session.execute(
                    text(
                        "SELECT 1 FROM decision_log WHERE event_type = 'telegram_unpaired' "
                        "ORDER BY seq DESC LIMIT 1"
                    )
                )
            ).first()
        assert logged is not None
    finally:
        await container.aclose()


# --- IDOR: business_id ajeno -> 404; el propietario nunca ve la fila de otro ---


async def test_get_pairing_404_for_a_business_the_caller_cannot_see(
    two_owners: _TwoOwners, database_url: str
) -> None:
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        async with _client(_app(container), raw_token=two_owners.owner_a.raw_token) as client:
            response = await client.get(
                "/api/v1/telegram/pairing", params={"business_id": str(uuid.uuid4())}
            )
        assert response.status_code == 404
    finally:
        await container.aclose()


async def test_pairing_state_is_never_leaked_across_owners(
    two_owners: _TwoOwners, database_url: str
) -> None:
    """El emparejamiento se resuelve por `owner_id` de la SESION, nunca por
    un identificador que el cliente pueda manipular -- el propietario B no
    puede ver ni el codigo ni el estado del propietario A."""
    settings = build_api_settings(database_url=database_url)
    container = Container.build(settings)
    try:
        app = _app(container)
        async with _client(app, raw_token=two_owners.owner_a.raw_token) as client_a:
            started = await confirmed_request(
                client_a, "POST", "/api/v1/telegram/pairing/start"
            )
            assert started.status_code == 201, started.text

        async with _client(app, raw_token=two_owners.owner_b.raw_token) as client_b:
            response = await client_b.get(
                "/api/v1/telegram/pairing",
                params={"business_id": str(two_owners.owner_b.business_id)},
            )
        body = response.json()
        assert body["status"] == "unpaired"
        assert body["pairing_code"] is None
    finally:
        await container.aclose()
