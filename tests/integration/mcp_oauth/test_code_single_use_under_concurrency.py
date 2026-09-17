"""Canje concurrente del mismo codigo de autorizacion (threat-model.md
C-38): dos transacciones reales, cada una con su propia conexion (mismo
patron que `tests/integration/execution/test_undo_execution_concurrency_sql.py`),
disputan el mismo `UPDATE ... WHERE state = 'CONSENTED'` sobre la misma
fila. Postgres serializa el `UPDATE` por el bloqueo de fila: la primera en
comprometer gana, la segunda ve `state = 'REDEEMED'` al reanudar y
`SqlAuthorizationRequestRepository.save()` lo traduce a
`CodeAlreadyRedeemedError` -- `RedeemCode.execute()` reacciona revocando la
concesion que la ganadora acaba de emitir."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from safent_ads.mcp_oauth.application.errors import UnknownAuthorizationCodeError
from safent_ads.mcp_oauth.application.redeem_code import IssuedTokenPair, RedeemCode
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.errors import CodeAlreadyRedeemedError
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.mcp_oauth.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRequestRepository,
)
from safent_ads.mcp_oauth.infrastructure.sql_client_repository import SqlClientRepository
from safent_ads.mcp_oauth.infrastructure.sql_grant_repository import SqlGrantRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import UuidIdGenerator

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_RESOURCE = ResourceIndicator("https://ads.example.com/mcp")
_REDIRECT_URI = "http://127.0.0.1:54321/callback"
_CLIENT_ID = "concurrent-client"


@dataclass(frozen=True, slots=True)
class _ConsentedCode:
    txn_id: uuid.UUID
    owner_id: uuid.UUID
    code: str
    code_verifier: str


@dataclass(frozen=True, slots=True)
class _Scenario:
    engine: AsyncEngine
    seeded: _ConsentedCode

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.engine.connect() as connection:
            session = AsyncSession(bind=connection, expire_on_commit=False)
            try:
                yield session
            finally:
                await session.rollback()
                await session.close()


def _pkce_pair() -> tuple[str, str]:
    verifier = "concurrent-code-verifier-0123456789abcdef"[:43]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


async def _seed_consented_request(session: AsyncSession) -> _ConsentedCode:
    hasher = Sha256TokenHasher()
    factory = SecretsOpaqueTokenFactory()
    code_verifier, code_challenge = _pkce_pair()
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

    txn_id = uuid.uuid4()
    request = AuthorizationRequest(
        txn_id=txn_id,
        client_id=_CLIENT_ID,
        redirect_uri=_REDIRECT_URI,
        code_challenge=code_challenge,
        client_state="xyz",
        scope_set=ScopeSet.parse("ads:read"),
        resource=_RESOURCE,
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=10),
    )
    requests = SqlAuthorizationRequestRepository(session, clock=FixedClock(_NOW))
    await requests.create(request)

    owner_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO owners (id, email, password_hash) VALUES "
            "(:id, :email, 'argon2id$fixture$not-a-real-hash')"
        ),
        {"id": str(owner_id), "email": f"owner-{owner_id.hex[:8]}@safent.example"},
    )

    code = factory.new_token()
    request.consent(
        owner_id=owner_id, code_hash=hasher.hash(code), now=_NOW, code_ttl=timedelta(seconds=60)
    )
    await requests.save(request)

    return _ConsentedCode(txn_id=txn_id, owner_id=owner_id, code=code, code_verifier=code_verifier)


def _build_redeem_code(session: AsyncSession) -> RedeemCode:
    return RedeemCode(
        authorization_requests=SqlAuthorizationRequestRepository(
            session, clock=FixedClock(_NOW + timedelta(seconds=1))
        ),
        grants=SqlGrantRepository(session),
        token_hasher=Sha256TokenHasher(),
        token_factory=SecretsOpaqueTokenFactory(),
        id_generator=UuidIdGenerator(),
        clock=FixedClock(_NOW + timedelta(seconds=1)),
    )


async def test_two_concurrent_redeems_of_the_same_code_yield_one_winner(
    isolated_database_url: str,
) -> None:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as setup_connection:
            setup = AsyncSession(bind=setup_connection, expire_on_commit=False)
            seeded = await _seed_consented_request(setup)
            await setup.commit()
            await setup.close()
        scenario = _Scenario(engine=engine, seeded=seeded)

        async with scenario.session() as first, scenario.session() as second:
            outcomes = await asyncio.gather(
                _redeem_with_the_real_verifier(first, seeded),
                _redeem_with_the_real_verifier(second, seeded),
                return_exceptions=True,
            )

        successes = [o for o in outcomes if isinstance(o, IssuedTokenPair)]
        failures = [o for o in outcomes if isinstance(o, CodeAlreadyRedeemedError)]
        assert len(successes) == 1
        assert len(failures) == 1

        async with scenario.session() as verify:
            grants = SqlGrantRepository(verify)
            grant = await grants.get_by_authorization_request_id(seeded.txn_id)
            assert grant is not None
            assert grant.is_revoked is True
            assert grant.revoked_reason == "authorization_code_replayed"
    finally:
        await engine.dispose()


async def _redeem_with_the_real_verifier(
    session: AsyncSession, seeded: _ConsentedCode
) -> IssuedTokenPair:
    """El commit va DENTRO de ambas ramas a proposito: el mismo motivo por
    el que `presentation/sdk_provider.py` (T009) debe comprometer la sesion
    incluso cuando `RedeemCode.execute()` levanta `CodeAlreadyRedeemedError`
    -- la revocacion de la familia que acaba de emitir la transaccion
    ganadora (C-38) sucede DENTRO de esta misma transaccion perdedora, y se
    perderia con un `rollback()` implicito al propagar la excepcion."""
    use_case = _build_redeem_code(session)
    try:
        result = await use_case.execute(
            code=seeded.code,
            redirect_uri=_REDIRECT_URI,
            client_id=_CLIENT_ID,
            code_verifier=seeded.code_verifier,
            resource=_RESOURCE.value,
        )
    except CodeAlreadyRedeemedError:
        await session.commit()
        raise
    await session.commit()
    return result


async def test_redeeming_an_unknown_code_raises_against_the_real_repository(
    isolated_database_url: str,
) -> None:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            session = AsyncSession(bind=connection, expire_on_commit=False)
            use_case = _build_redeem_code(session)
            with pytest.raises(UnknownAuthorizationCodeError):
                await use_case.execute(
                    code="never-issued",  # noqa: S106 - fixture, no secreto real
                    redirect_uri=_REDIRECT_URI,
                    client_id=_CLIENT_ID,
                    code_verifier="x" * 43,
                    resource=_RESOURCE.value,
                )
            await session.rollback()
            await session.close()
    finally:
        await engine.dispose()
