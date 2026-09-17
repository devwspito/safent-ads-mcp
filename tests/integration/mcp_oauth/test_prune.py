"""`prune_stale_oauth_state` (tasks.md T016, threat-model.md C-42/C-58,
data-model.md "Retencion") contra Postgres real:

1. Cliente sin consentir (`last_seen_at IS NULL`) de mas de 24h se borra;
   uno mas joven o uno TRUSTED (con `last_seen_at`) sobreviven sin importar
   su edad.
2. Una solicitud PENDING cuyo plazo ya paso se marca EXPIRED (sin
   borrarla todavia); una terminal (DENIED/REDEEMED/EXPIRED) de mas de 7
   dias se borra, una mas joven sobrevive.
3. Un token caducado de mas de 30 dias se borra; uno caducado pero mas
   joven sobrevive (todavia dentro de la ventana de retencion).
4. Una transaccion federada caducada se borra (tasks.md T055); una que
   todavia vive sobrevive."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.grant import Grant, IssuedToken, TokenKind
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.prune_stale_clients import prune_stale_oauth_state
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.mcp_oauth.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRequestRepository,
)
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import SqlClientRepository
from safent_ads.mcp_oauth.infrastructure.sql_grant_repository import SqlGrantRepository
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_RESOURCE = ResourceIndicator("https://ads.test.ts.net/mcp")
_CODE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
_REDIRECT_URI = "http://127.0.0.1:54321/callback"
_HASHER = Sha256TokenHasher()
# Ver `test_consent_router.py::_cleanup_seeded_clients`: prefijo propio de
# este fichero, tanto para clientes (CASCADE hasta solicitudes/concesiones/
# tokens) como para el email del propietario que siembra
# `test_expired_token_retention`.
_CLIENT_ID_PREFIX = "client-prune-test-"
_OWNER_EMAIL_PREFIX = "prune-test-owner-"


@pytest.fixture
async def session_factory(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_seeded_rows(database_url: str) -> AsyncIterator[None]:
    yield
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM oauth_clients WHERE client_id LIKE :prefix"),
            {"prefix": f"{_CLIENT_ID_PREFIX}%"},
        )
        await connection.execute(
            text("DELETE FROM owners WHERE email LIKE :prefix"),
            {"prefix": f"{_OWNER_EMAIL_PREFIX}%"},
        )
    await engine.dispose()


def _client(client_id: str, *, created_at: datetime, last_seen_at: datetime | None) -> OAuthClient:
    return OAuthClient(
        client_id=client_id,
        client_name="Claude Code",
        redirect_uris=(RedirectUri(_REDIRECT_URI),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read"),
        created_at=created_at,
        last_seen_at=last_seen_at,
    )


def _pending_request(
    client_id: str, *, created_at: datetime, expires_at: datetime
) -> AuthorizationRequest:
    return AuthorizationRequest(
        txn_id=uuid.uuid4(),
        client_id=client_id,
        redirect_uri=_REDIRECT_URI,
        code_challenge=_CODE_CHALLENGE,
        client_state=None,
        scope_set=ScopeSet.parse("ads:read"),
        resource=_RESOURCE,
        created_at=created_at,
        expires_at=expires_at,
    )


async def _seed_owner(session: AsyncSession) -> uuid.UUID:
    owner_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, :password_hash)"),
        {
            "id": str(owner_id),
            "email": f"{_OWNER_EMAIL_PREFIX}{owner_id.hex[:8]}@safent.example",
            "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
        },
    )
    return owner_id


async def _get_client_state(
    session_factory: async_sessionmaker[AsyncSession], client_id: str
) -> str | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT client_id FROM oauth_clients WHERE client_id = :id"),
                {"id": client_id},
            )
        ).one_or_none()
    return None if row is None else row.client_id


async def _get_request_state(
    session_factory: async_sessionmaker[AsyncSession], txn_id: uuid.UUID
) -> str | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT state FROM oauth_authorization_requests WHERE txn_id = :id"),
                {"id": str(txn_id)},
            )
        ).one_or_none()
    return None if row is None else row.state


async def _get_token_state(
    session_factory: async_sessionmaker[AsyncSession], token_hash: str
) -> str | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT token_hash FROM oauth_tokens WHERE token_hash = :h"),
                {"h": token_hash},
            )
        ).one_or_none()
    return None if row is None else row.token_hash


async def test_unconsented_client_older_than_24h_is_deleted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    old_id = f"{_CLIENT_ID_PREFIX}old-{uuid.uuid4().hex[:8]}"
    young_id = f"{_CLIENT_ID_PREFIX}young-{uuid.uuid4().hex[:8]}"
    trusted_id = f"{_CLIENT_ID_PREFIX}trusted-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        repo = SqlClientRepository(session)
        await repo.save(_client(old_id, created_at=_NOW - timedelta(hours=25), last_seen_at=None))
        await repo.save(_client(young_id, created_at=_NOW - timedelta(hours=1), last_seen_at=None))
        await repo.save(
            _client(
                trusted_id,
                created_at=_NOW - timedelta(hours=200),
                last_seen_at=_NOW - timedelta(hours=1),
            )
        )
        await session.commit()

    await prune_stale_oauth_state(session_factory, clock=FixedClock(_NOW))

    assert await _get_client_state(session_factory, old_id) is None
    assert await _get_client_state(session_factory, young_id) == young_id
    assert await _get_client_state(session_factory, trusted_id) == trusted_id


async def test_stale_pending_request_transitions_to_expired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id = f"{_CLIENT_ID_PREFIX}{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        await SqlClientRepository(session).save(
            _client(client_id, created_at=_NOW - timedelta(minutes=30), last_seen_at=None)
        )
        request = _pending_request(
            client_id,
            created_at=_NOW - timedelta(minutes=11),
            expires_at=_NOW - timedelta(minutes=1),
        )
        await SqlAuthorizationRequestRepository(session, clock=FixedClock(_NOW)).create(request)
        await session.commit()

    await prune_stale_oauth_state(session_factory, clock=FixedClock(_NOW))

    assert await _get_request_state(session_factory, request.id) == "EXPIRED"


async def test_terminal_request_retention(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id = f"{_CLIENT_ID_PREFIX}{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        # `last_seen_at` fijado (TRUSTED): si no, la poda del CLIENTE (mas
        # de 24h sin consentir) se lo llevaria por delante con CASCADE,
        # borrando ambas solicitudes y confundiendo lo que prueba este test.
        await SqlClientRepository(session).save(
            _client(client_id, created_at=_NOW - timedelta(days=10), last_seen_at=_NOW)
        )
        old_created_at = _NOW - timedelta(days=8)
        recent_created_at = _NOW - timedelta(days=1)
        old_terminal = _pending_request(
            client_id, created_at=old_created_at, expires_at=old_created_at + timedelta(minutes=10)
        )
        recent_terminal = _pending_request(
            client_id,
            created_at=recent_created_at,
            expires_at=recent_created_at + timedelta(minutes=10),
        )
        clock = FixedClock(_NOW)
        requests = SqlAuthorizationRequestRepository(session, clock=clock)
        await requests.create(old_terminal)
        await requests.create(recent_terminal)
        # Denegar justo antes de su propio plazo para no chocar con
        # `_reject_if_expired` (el reloj de dominio) NI con el `expires_at
        # > :now` que la capa SQL ahora tambien exige (M5) -- se adelanta
        # el reloj del repositorio al mismo instante, uno por cada save().
        old_terminal.deny(old_terminal.expires_at - timedelta(seconds=1))
        recent_terminal.deny(recent_terminal.expires_at - timedelta(seconds=1))
        clock.advance_to(old_terminal.expires_at - timedelta(seconds=1))
        await requests.save(old_terminal)
        clock.advance_to(recent_terminal.expires_at - timedelta(seconds=1))
        await requests.save(recent_terminal)
        await session.commit()

    await prune_stale_oauth_state(session_factory, clock=FixedClock(_NOW))

    assert await _get_request_state(session_factory, old_terminal.id) is None
    assert await _get_request_state(session_factory, recent_terminal.id) == "DENIED"


def _federated_state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


async def _insert_federated_login_transaction(
    session: AsyncSession, *, state: str, created_at: datetime, expires_at: datetime
) -> None:
    await session.execute(
        text(
            "INSERT INTO federated_login_transactions "
            "(state_hash, nonce_hash, purpose, session_id, txn_id, created_at, expires_at) "
            "VALUES (:state_hash, :nonce_hash, 'login', NULL, NULL, :created_at, :expires_at)"
        ),
        {
            "state_hash": _federated_state_hash(state),
            "nonce_hash": _federated_state_hash(f"nonce-{state}"),
            "created_at": created_at,
            "expires_at": expires_at,
        },
    )


async def _get_federated_login_transaction(
    session_factory: async_sessionmaker[AsyncSession], state: str
) -> str | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT state_hash FROM federated_login_transactions WHERE state_hash = :h"),
                {"h": _federated_state_hash(state)},
            )
        ).one_or_none()
    return None if row is None else row.state_hash


async def test_expired_federated_login_transaction_is_deleted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _insert_federated_login_transaction(
            session,
            state="prune-test-expired-state",
            created_at=_NOW - timedelta(minutes=20),
            expires_at=_NOW - timedelta(minutes=1),
        )
        await _insert_federated_login_transaction(
            session,
            state="prune-test-live-state",
            created_at=_NOW - timedelta(minutes=1),
            expires_at=_NOW + timedelta(minutes=9),
        )
        await session.commit()

    try:
        report = await prune_stale_oauth_state(session_factory, clock=FixedClock(_NOW))

        assert (
            await _get_federated_login_transaction(session_factory, "prune-test-expired-state")
            is None
        )
        assert await _get_federated_login_transaction(
            session_factory, "prune-test-live-state"
        ) == _federated_state_hash("prune-test-live-state")
        assert report.deleted_federated_login_transactions == 1
    finally:
        async with session_factory() as session:
            await session.execute(
                text("DELETE FROM federated_login_transactions WHERE state_hash = :h"),
                {"h": _federated_state_hash("prune-test-live-state")},
            )
            await session.commit()


async def test_expired_token_retention(session_factory: async_sessionmaker[AsyncSession]) -> None:
    client_id = f"{_CLIENT_ID_PREFIX}{uuid.uuid4().hex[:8]}"
    old_token_raw = f"old-expired-token-{uuid.uuid4().hex}"  # noqa: S105 - fixture
    recent_token_raw = f"recent-expired-token-{uuid.uuid4().hex}"  # noqa: S105 - fixture
    old_hash = str(_HASHER.hash(old_token_raw))
    recent_hash = str(_HASHER.hash(recent_token_raw))

    async with session_factory() as session:
        owner_id = await _seed_owner(session)
        await SqlClientRepository(session).save(
            _client(client_id, created_at=_NOW - timedelta(days=100), last_seen_at=_NOW)
        )
        request = _pending_request(
            client_id,
            created_at=_NOW - timedelta(minutes=30),
            expires_at=_NOW + timedelta(minutes=30),
        )
        await SqlAuthorizationRequestRepository(session, clock=FixedClock(_NOW)).create(request)
        grant = Grant(
            grant_id=uuid.uuid4(),
            authorization_request_id=request.id,
            owner_id=owner_id,
            client_id=client_id,
            scope_set=ScopeSet.parse("ads:read"),
            resource=_RESOURCE,
            created_at=_NOW - timedelta(days=100),
            tokens=(
                IssuedToken(
                    token_hash=_HASHER.hash(old_token_raw),
                    kind=TokenKind.ACCESS,
                    issued_at=_NOW - timedelta(days=131),
                    expires_at=_NOW - timedelta(days=31),
                ),
                IssuedToken(
                    token_hash=_HASHER.hash(recent_token_raw),
                    kind=TokenKind.REFRESH,
                    issued_at=_NOW - timedelta(days=2),
                    expires_at=_NOW - timedelta(days=1),
                ),
            ),
        )
        await SqlGrantRepository(session).create(grant)
        await session.commit()

    await prune_stale_oauth_state(session_factory, clock=FixedClock(_NOW))

    assert await _get_token_state(session_factory, old_hash) is None
    assert await _get_token_state(session_factory, recent_hash) == recent_hash
