"""Community owner login: durable password lockout, sessions without OTP."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pyotp
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.composition.app import create_app
from safent_ads.composition.settings import ApiSettings
from safent_ads.iam.application.errors import AccountLockedError, InvalidCredentialsError
from safent_ads.iam.application.login import Login
from safent_ads.iam.application.session_policy import LOCKOUT_THRESHOLD, LOCKOUT_WINDOW
from safent_ads.iam.infrastructure.aesgcm_totp_cipher import (
    PURPOSE_TOTP_SECRET,
    AesGcmTotpCipher,
)
from safent_ads.iam.infrastructure.argon2_password_hasher import Argon2PasswordHasher
from safent_ads.iam.infrastructure.sql_login_attempt_repository import SqlLoginAttemptRepository
from safent_ads.iam.infrastructure.sql_owner_repository import SqlOwnerRepository
from safent_ads.iam.infrastructure.sql_session_repository import SqlSessionRepository
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import UuidIdGenerator

pytestmark = pytest.mark.integration

_VALID_32_BYTE_KEY_B64 = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
_SESSION_SECRET = "test-session-secret-0123456789abcdef"  # noqa: S105 - fixture, no secreto real
_CORRECT_PASSWORD = "correct horse battery staple"  # noqa: S105 - fixture, no secreto real
_WRONG_PASSWORD = "wrong horse battery staple"  # noqa: S105 - fixture, no secreto real


class _SeededOwner:
    def __init__(self, *, owner_id: uuid.UUID, email: str, totp_secret: str) -> None:
        self.owner_id = owner_id
        self.email = email
        self.totp_secret = totp_secret


@pytest.fixture
async def seeded_owner(database_url: str) -> AsyncIterator[_SeededOwner]:
    """Escribe con commit real por su motor propio: `Container` abre su
    propio pool de conexiones, asi que la fila tiene que estar confirmada
    para que la vea (mismo patron que
    `tests/integration/brand/test_authorization_integration.py::seeded_owner`)."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    owner_id = uuid.uuid4()
    email = f"owner-{owner_id.hex[:10]}@safent.example"
    totp_secret = pyotp.random_base32()
    password_hash = Argon2PasswordHasher().hash(_CORRECT_PASSWORD)
    encrypted_secret = AesGcmTotpCipher(_VALID_32_BYTE_KEY_B64).encrypt(
        totp_secret, purpose=PURPOSE_TOTP_SECRET
    )
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO owners "
                "(id, email, password_hash, totp_secret_encrypted, totp_confirmed_at) "
                "VALUES (:id, :email, :password_hash, :totp_secret, :now)"
            ),
            {
                "id": str(owner_id),
                "email": email,
                "password_hash": password_hash,
                "totp_secret": encrypted_secret,
                "now": now,
            },
        )
    try:
        yield _SeededOwner(owner_id=owner_id, email=email, totp_secret=totp_secret)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM login_attempts WHERE email = :email"), {"email": email}
            )
            # `test_successful_login_resets_the_lockout_counter` completa un
            # login real (password + TOTP) y deja una fila en `sessions`
            # referenciando a este owner -- sin borrarla primero,
            # `DELETE FROM owners` choca con `sessions_owner_id_fkey`.
            await connection.execute(
                text("DELETE FROM sessions WHERE owner_id = :id"), {"id": str(owner_id)}
            )
            await connection.execute(
                text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)}
            )
        await engine.dispose()


def _api_settings(database_url: str) -> ApiSettings:
    return ApiSettings(
        database_url=database_url,
        session_secret=_SESSION_SECRET,
        totp_enc_key=_VALID_32_BYTE_KEY_B64,
        mcp_token="test-mcp-token-abc123",
        broker_socket_path="/tmp/safent-ads-test/lockout.sock",
        public_base_url="https://ads.test.ts.net",
        telegram_bot_token="123456:test-bot-token",
        telegram_owner_chat_ids=[111222333],
        approval_signing_key=_VALID_32_BYTE_KEY_B64,
        seat_authority_enabled=True,
        enterprise_origin="https://enterprise.test",
        enterprise_service_secret="a" * 64,
        enterprise_org_ids=frozenset({uuid.UUID("00000000-0000-0000-0000-000000000001")}),
    )


@asynccontextmanager
async def _client_with_csrf(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        await client.get("/api/v1/health")
        client.headers["X-CSRF-Token"] = client.cookies["ads_csrf"]
        yield client


async def test_login_locks_out_after_five_wrong_passwords_and_rejects_correct_password(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    app = create_app(_api_settings(database_url))
    try:
        async with _client_with_csrf(app) as client:
            for _ in range(LOCKOUT_THRESHOLD):
                response = await client.post(
                    "/api/v1/auth/login",
                    json={"email": seeded_owner.email, "password": _WRONG_PASSWORD},
                )
                assert response.status_code == 401, response.text
                assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

            # El sexto intento trae la contrasena CORRECTA: antes del fix se
            # aceptaba (200, reto TOTP) porque ninguno de los cinco fallos
            # anteriores habia sobrevivido al rollback de su peticion.
            locked = await client.post(
                "/api/v1/auth/login",
                json={"email": seeded_owner.email, "password": _CORRECT_PASSWORD},
            )
        assert locked.status_code == 429, locked.text
        assert locked.json()["error"]["code"] == "ACCOUNT_LOCKED"
    finally:
        await app.state.container.aclose()


async def test_password_session_requires_no_totp_and_old_endpoint_is_gone(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    app = create_app(_api_settings(database_url))
    try:
        async with _client_with_csrf(app) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": seeded_owner.email, "password": _CORRECT_PASSWORD},
            )
            assert login.status_code == 204
            assert SESSION_COOKIE_NAME in login.cookies
            assert (await client.get("/api/v1/auth/me")).status_code == 200
            gone = await client.post(
                "/api/v1/auth/totp", json={"challenge_id": "obsolete", "code": "123456"}
            )
            assert gone.status_code == 404
            assert (await client.post("/api/v1/auth/logout")).status_code == 204
            assert (await client.get("/api/v1/auth/me")).status_code == 401
    finally:
        await app.state.container.aclose()


async def test_successful_login_resets_the_lockout_counter(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    """Un exito no cuenta como fallo (`SqlLoginAttemptRepository.
    count_recent_failures` filtra `succeeded = false`): tras algunos
    fallos POR DEBAJO del umbral seguidos de un login completo (password +
    TOTP) correcto, el propietario puede volver a entrar de inmediato --
    los fallos previos mas el exito nunca llegan a `LOCKOUT_THRESHOLD`."""
    below_threshold_failures = LOCKOUT_THRESHOLD - 2
    app = create_app(_api_settings(database_url))
    try:
        async with _client_with_csrf(app) as client:
            for _ in range(below_threshold_failures):
                response = await client.post(
                    "/api/v1/auth/login",
                    json={"email": seeded_owner.email, "password": _WRONG_PASSWORD},
                )
                assert response.status_code == 401, response.text

            first_login = await client.post(
                "/api/v1/auth/login",
                json={"email": seeded_owner.email, "password": _CORRECT_PASSWORD},
            )
            assert first_login.status_code == 204, first_login.text
            assert SESSION_COOKIE_NAME in first_login.cookies

            second_login = await client.post(
                "/api/v1/auth/login",
                json={"email": seeded_owner.email, "password": _CORRECT_PASSWORD},
            )
        assert second_login.status_code == 204, second_login.text
    finally:
        await app.state.container.aclose()


async def test_login_lockout_expires_after_fifteen_minutes(
    seeded_owner: _SeededOwner, database_url: str
) -> None:
    """Mismos adaptadores SQL que produccion, sesion nueva por llamada
    (`iam/presentation/router.py` abre y cierra una por peticion): prueba
    que el fix persiste los fallos a traves de sesiones EFIMERAS y que
    `count_recent_failures` deja de contarlos pasados los 15 minutos de
    `LOCKOUT_WINDOW`, con un reloj falso que no depende de esperar de
    verdad."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    # `login_attempts.attempted_at` es `DEFAULT now()` en Postgres
    # (0001_bootstrap.py): el reloj de la fila la pone el servidor, no el
    # `Clock` de la aplicacion. El reloj falso arranca en el "ahora" real
    # para que `since = clock.now() - LOCKOUT_WINDOW` sea comparable con esas
    # filas -- solo `advance_to` mueve la ventana hacia adelante de verdad.
    clock = FixedClock(datetime.now(UTC))
    hasher = Argon2PasswordHasher()
    decoy_hash = hasher.hash("decoy-password")
    ip_address = "203.0.113.55"

    async def attempt(password: str):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            login = Login(
                owner_repository=SqlOwnerRepository(session),
                login_attempt_repository=SqlLoginAttemptRepository(session),
                password_hasher=hasher,
                session_repository=SqlSessionRepository(session),
                id_generator=UuidIdGenerator(),
                clock=clock,
                decoy_password_hash=decoy_hash,
            )
            return await login.execute(
                email=seeded_owner.email, password=password, ip_address=ip_address
            )

    try:
        for _ in range(LOCKOUT_THRESHOLD):
            with pytest.raises(InvalidCredentialsError):
                await attempt(_WRONG_PASSWORD)

        with pytest.raises(AccountLockedError):
            await attempt(_CORRECT_PASSWORD)

        clock.advance_to(clock.now() + LOCKOUT_WINDOW + timedelta(seconds=1))

        challenge = await attempt(_CORRECT_PASSWORD)
        assert challenge.raw_token
    finally:
        await engine.dispose()
