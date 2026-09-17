"""L7 de la revision de seguridad (16-sep): dos refrescos concurrentes del
MISMO refresh token, cada uno con su propia conexion real (mismo patron que
`test_code_single_use_under_concurrency.py`).

Bug fix/refresh-rotation-race (16-sep, flaky ~1/25 localmente, peor en CI):
`asyncio.gather` por si solo NO garantiza que las dos transacciones se
disputen nada de verdad -- es igual de valido, en cuanto al scheduling de
asyncio, que UNA de las dos (SELECT + rotar + 4 UPSERT + commit) termine
ENTERA antes de que la otra ejecute su primer SELECT. Cuando pasaba eso,
la perdedora leia el refresh YA ROTATED desde el principio:
`Grant.rotate_refresh_token()` (`domain/grant.py` linea ~150) lo trataba
como un reuso genuino y revocaba la concesion ENTERA (`revoke()`, linea
~151), incluidos los tokens recien emitidos de la ganadora -- de ahi el
`assert len(active_refresh_tokens) == 1` fallando con `0 == 1` de forma
intermitente, aunque `successes`/`failures` ya salian bien (1 y 1).

`_ReadRendezvous` de aqui abajo fuerza el entrelazado real SIN sleeps:
cada lado espera a que el otro tambien haya intentado reservar la fila
del refresh antes de seguir. Con el fix
(`SqlGrantRepository.get_by_token_hash_for_rotation`, `FOR UPDATE NOWAIT`
sobre la fila del refresh ANTES de leerla) la perdedora ya no llega a leer
nada: Postgres rechaza su reserva al instante (55P03) mientras la
ganadora sigue con la suya en curso, y `SdkOAuthProvider._refresh()`
traduce eso (`ConcurrentRefreshInProgressError`, o en su defecto el
`IntegrityError` de `ix_oauth_tokens_grant_active` como red de seguridad)
a `TokenError("invalid_grant", …)` sin tocar la familia de la ganadora."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mcp.server.auth.provider import RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from safent_ads.mcp_oauth.application.token_issuance import mint_access_and_refresh
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.grant import Grant, TokenKind, TokenState
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.mcp_oauth.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRequestRepository,
)
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import SqlClientRepository
from safent_ads.mcp_oauth.infrastructure.sql_grant_repository import SqlGrantRepository
from safent_ads.mcp_oauth.infrastructure.sql_oauth_session import SqlOAuthSession
from safent_ads.mcp_oauth.presentation.sdk_provider import SdkOAuthProvider
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import UuidIdGenerator

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_RESOURCE = ResourceIndicator("https://ads.example.com/mcp")
_REDIRECT_URI = "http://127.0.0.1:54321/callback"
_CLIENT_ID = "concurrent-refresh-client"
_PUBLIC_BASE_URL = "https://ads.example.com"


@dataclass(frozen=True, slots=True)
class _SeededGrant:
    grant_id: uuid.UUID
    owner_id: uuid.UUID
    refresh_token: str


async def _seed_grant_with_active_refresh(session: AsyncSession) -> _SeededGrant:
    hasher = Sha256TokenHasher()
    factory = SecretsOpaqueTokenFactory()
    client = OAuthClient(
        client_id=_CLIENT_ID,
        client_name="Claude Code",
        redirect_uris=(RedirectUri(_REDIRECT_URI),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read"),
        created_at=_NOW,
    )
    await SqlClientRepository(session).save(client)

    owner_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO owners (id, email, password_hash) VALUES "
            "(:id, :email, 'argon2id$fixture$not-a-real-hash')"
        ),
        {"id": str(owner_id), "email": f"owner-{owner_id.hex[:8]}@safent.example"},
    )
    # `oauth_grants.authorization_txn_id` exige una fila real en
    # `oauth_authorization_requests` (FK) -- PENDING basta, el estado no
    # importa para este test, solo que exista (mismo patron que
    # `test_grants_router.py::_seed_client_and_grant`).
    authorization_request = AuthorizationRequest(
        txn_id=uuid.uuid4(),
        client_id=_CLIENT_ID,
        redirect_uri=_REDIRECT_URI,
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        client_state=None,
        scope_set=ScopeSet.parse("ads:read"),
        resource=_RESOURCE,
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=10),
    )
    await SqlAuthorizationRequestRepository(session, clock=FixedClock(_NOW)).create(
        authorization_request
    )

    access, refresh = mint_access_and_refresh(now=_NOW, factory=factory, hasher=hasher)
    grant = Grant(
        grant_id=uuid.uuid4(),
        authorization_request_id=authorization_request.id,
        owner_id=owner_id,
        client_id=_CLIENT_ID,
        scope_set=ScopeSet.parse("ads:read"),
        resource=_RESOURCE,
        created_at=_NOW,
        tokens=(access.issued, refresh.issued),
    )
    await SqlGrantRepository(session).create(grant)

    return _SeededGrant(grant_id=grant.id, owner_id=owner_id, refresh_token=refresh.raw_value)


class _ReadRendezvous:
    """Barrera de dos partes sin sleeps: cada lado avisa que ya INTENTO
    reservar la fila del refresh (`arrive_and_wait_for`) -- gane o pierda
    esa reserva -- y espera a que el otro TAMBIEN lo haya intentado antes
    de que ninguno de los dos siga. Fuerza el unico entrelazado que
    reproduce la carrera de verdad (los dos intentan mientras el otro
    sigue en vuelo) en vez de dejarlo a la suerte del scheduler de
    asyncio."""

    def __init__(self) -> None:
        self._arrived = asyncio.Event()

    async def arrive_and_wait_for(self, other: _ReadRendezvous) -> None:
        self._arrived.set()
        await other._arrived.wait()  # noqa: SLF001 - las dos mitades del mismo par


class _RendezvousGrantRepository:
    """Envuelve un `GrantRepository` real: tras intentar
    `get_by_token_hash_for_rotation` -- la unica lectura de
    `RefreshGrant.execute()` antes de mutar el agregado en memoria --
    espera en la barrera antes de devolver el resultado (o dejar propagar
    el fallo) al caso de uso, para que la otra transaccion tenga la
    garantia de haber intentado tambien la suya antes de que esta siga."""

    def __init__(self, inner: Any, rendezvous: _ReadRendezvous, partner: _ReadRendezvous) -> None:
        self._inner = inner
        self._rendezvous = rendezvous
        self._partner = partner

    async def get_by_token_hash_for_rotation(self, token_hash: Any) -> Any:
        try:
            return await self._inner.get_by_token_hash_for_rotation(token_hash)
        finally:
            await self._rendezvous.arrive_and_wait_for(self._partner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _RendezvousOAuthSession(SqlOAuthSession):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: FixedClock,
        rendezvous: _ReadRendezvous,
        partner: _ReadRendezvous,
    ) -> None:
        super().__init__(session_factory, clock)
        self._rendezvous = rendezvous
        self._partner = partner

    async def __aenter__(self) -> Any:
        session = await super().__aenter__()
        session.grants = _RendezvousGrantRepository(  # type: ignore[assignment]
            session.grants, self._rendezvous, self._partner
        )
        return session


def _provider_bound_to(
    connection: AsyncEngine, *, rendezvous: tuple[_ReadRendezvous, _ReadRendezvous] | None = None
) -> SdkOAuthProvider:
    """Una `SdkOAuthProvider` por conexion REAL propia (mismo motivo que
    `_Scenario.session()` en `test_code_single_use_under_concurrency.py`):
    sin esto, las dos llamadas concurrentes compartirian sesion/conexion y
    nunca se disputarian nada de verdad. `rendezvous=(mine, partner)`
    sincroniza el intento de reserva con el del otro lado (ver
    `_ReadRendezvous`)."""
    session_maker = async_sessionmaker(bind=connection, expire_on_commit=False)
    clock = FixedClock(_NOW + timedelta(days=1))
    if rendezvous is None:
        session_factory = lambda: SqlOAuthSession(session_maker, clock)  # noqa: E731
    else:
        mine, partner = rendezvous
        session_factory = lambda: _RendezvousOAuthSession(  # noqa: E731
            session_maker, clock, mine, partner
        )
    return SdkOAuthProvider(
        session_factory=session_factory,
        id_generator=UuidIdGenerator(),
        clock=clock,
        token_hasher=Sha256TokenHasher(),
        token_factory=SecretsOpaqueTokenFactory(),
        public_base_url=_PUBLIC_BASE_URL,
    )


async def _refresh(provider: SdkOAuthProvider, raw_refresh_token: str) -> object:
    client = OAuthClientInformationFull(
        client_id=_CLIENT_ID,
        redirect_uris=[_REDIRECT_URI],  # type: ignore[list-item]
        token_endpoint_auth_method="none",  # noqa: S106 - metodo RFC 7591, no un secreto
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="ads:read",
    )
    refresh_token = RefreshToken(
        token=raw_refresh_token,
        client_id=_CLIENT_ID,
        scopes=["ads:read"],
        resource=_RESOURCE.value,
    )
    try:
        return await provider.exchange_refresh_token(client, refresh_token, scopes=[])
    except TokenError as exc:
        return exc


@asynccontextmanager
async def _connection(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    async with engine.connect() as connection:
        yield connection


async def test_two_concurrent_refreshes_of_the_same_token_yield_one_winner(
    isolated_database_url: str,
) -> None:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as setup_connection:
            setup_session = AsyncSession(bind=setup_connection, expire_on_commit=False)
            seeded = await _seed_grant_with_active_refresh(setup_session)
            await setup_session.commit()
            await setup_session.close()

        async with _connection(engine) as first, _connection(engine) as second:
            rendezvous_first = _ReadRendezvous()
            rendezvous_second = _ReadRendezvous()
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    _refresh(
                        _provider_bound_to(first, rendezvous=(rendezvous_first, rendezvous_second)),
                        seeded.refresh_token,
                    ),
                    _refresh(
                        _provider_bound_to(
                            second, rendezvous=(rendezvous_second, rendezvous_first)
                        ),
                        seeded.refresh_token,
                    ),
                ),
                timeout=10,
            )

        successes = [o for o in outcomes if not isinstance(o, TokenError)]
        failures = [o for o in outcomes if isinstance(o, TokenError)]
        assert len(successes) == 1, outcomes
        assert len(failures) == 1, outcomes
        assert failures[0].error == "invalid_grant"

        async with engine.connect() as verify_connection:
            verify_session = AsyncSession(bind=verify_connection, expire_on_commit=False)
            grant = await SqlGrantRepository(verify_session).get_by_id(seeded.grant_id)
            assert grant is not None
            active_refresh_tokens = [
                token
                for token in grant.tokens
                if token.kind is TokenKind.REFRESH and token.state is TokenState.ACTIVE
            ]
            assert len(active_refresh_tokens) == 1
    finally:
        await engine.dispose()
