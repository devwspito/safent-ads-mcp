"""Contract regression (spec 002b tasks.md T072, FR-122, decision 8): the
static bearer (`ADS_MCP_TOKEN`) keeps working when the OAuth surface is
ALSO enabled, through the exact SAME `CompositeTokenVerifier`
(threat-model.md C-48/C-53: "un solo verificador, nunca uno segundo") that
authenticates an OAuth-granted call -- and the two are distinguishable by
`AccessToken.client_id`, the field `caller_scope.py::OAuthCallerScopeResolver`
uses to route to `caller_id="owner"` (static) vs
`caller_id="oauth:{client_id}:{subject}"` (OAuth), which is exactly what
ends up in the tool-call audit trail (`mcp/presentation/dispatcher.py`).

Nothing in 002b touches `token_verifier.py`/`caller_scope.py`: this is a
regression guard, not new behaviour."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.composition.container import Container
from safent_ads.mcp_oauth.application.token_issuance import mint_access_and_refresh
from safent_ads.mcp_oauth.domain.authorization import AuthorizationRequest
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.grant import Grant
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
from safent_ads.mcp_oauth.presentation.token_verifier import (
    STATIC_CALLER_CLIENT_ID,
    CompositeTokenVerifier,
)
from safent_ads.shared.clock import SystemClock
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_PUBLIC_BASE_URL = "https://ads.test.ts.net"
_RESOURCE = ResourceIndicator(f"{_PUBLIC_BASE_URL}/mcp")
_REDIRECT_URI = "http://127.0.0.1:54321/callback"
_STATIC_TOKEN = "contract-test-static-token-abc123"  # noqa: S105 - fixture, not a real secret
_CLIENT_ID_PREFIX = "client-static-vs-oauth-contract-"


async def _seed_owner(session: AsyncSession) -> uuid.UUID:
    owner_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO owners (id, email, password_hash) VALUES (:id, :email, :password_hash)"),
        {
            "id": str(owner_id),
            "email": f"{_CLIENT_ID_PREFIX}{owner_id.hex[:8]}@safent.example",
            "password_hash": "argon2id$fixture$not-a-real-hash",  # noqa: S106
        },
    )
    return owner_id


async def _seed_client_and_grant(database_url: str) -> tuple[str, uuid.UUID, str]:
    """Same shape as `test_grants_router.py::_seed_client_and_grant`,
    trimmed to just the access token this contract needs."""
    engine = create_async_engine(database_url, pool_pre_ping=True)
    client = OAuthClient(
        client_id=f"{_CLIENT_ID_PREFIX}{uuid.uuid4().hex[:8]}",
        client_name="Claude Code",
        redirect_uris=(RedirectUri(_REDIRECT_URI),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code", "refresh_token"),
        requested_scope=ScopeSet.parse("ads:read"),
        created_at=datetime.now(UTC),
    )
    hasher = Sha256TokenHasher()
    factory = SecretsOpaqueTokenFactory()
    now = datetime.now(UTC)
    access, refresh = mint_access_and_refresh(now=now, factory=factory, hasher=hasher)
    authorization_request = AuthorizationRequest(
        txn_id=uuid.uuid4(),
        client_id=client.id,
        redirect_uri=_REDIRECT_URI,
        code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        client_state=None,
        scope_set=ScopeSet.parse("ads:read"),
        resource=_RESOURCE,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        owner_id = await _seed_owner(session)
        await SqlClientRepository(session).save(client)
        await SqlAuthorizationRequestRepository(session, clock=SystemClock()).create(
            authorization_request
        )
        grant = Grant(
            grant_id=uuid.uuid4(),
            authorization_request_id=authorization_request.id,
            owner_id=owner_id,
            client_id=client.id,
            scope_set=ScopeSet.parse("ads:read"),
            resource=_RESOURCE,
            created_at=now,
            tokens=(access.issued,),
        )
        await SqlGrantRepository(session).create(grant)
        await session.commit()
    await engine.dispose()
    return client.id, owner_id, access.raw_value


async def _cleanup(database_url: str, *, client_id: str, owner_id: uuid.UUID) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM oauth_clients WHERE client_id = :id"), {"id": client_id}
        )
        await connection.execute(text("DELETE FROM owners WHERE id = :id"), {"id": str(owner_id)})
    await engine.dispose()


async def test_static_and_oauth_calls_are_distinguishable_through_one_verifier(
    database_url: str,
) -> None:
    client_id, owner_id, access_token = await _seed_client_and_grant(database_url)
    settings = build_api_settings(
        database_url=database_url,
        public_base_url=_PUBLIC_BASE_URL,
        mcp_static_token_enabled=True,
        mcp_token=_STATIC_TOKEN,
    )
    container = Container.build(settings)
    verifier = CompositeTokenVerifier(
        session_factory=lambda: SqlOAuthSession(container.session_factory, SystemClock()),
        token_hasher=Sha256TokenHasher(),
        clock=SystemClock(),
        static_token=_STATIC_TOKEN,
        resource=_RESOURCE.value,
    )
    try:
        static_result = await verifier.verify_token(_STATIC_TOKEN)
        oauth_result = await verifier.verify_token(access_token)
    finally:
        await container.aclose()
        await _cleanup(database_url, client_id=client_id, owner_id=owner_id)

    assert static_result is not None
    assert oauth_result is not None
    # FR-122: el estatico sigue funcionando con OAuth encendido a la vez.
    assert static_result.client_id == STATIC_CALLER_CLIENT_ID
    # `caller_scope.py::OAuthCallerScopeResolver` decide por ESTE campo:
    # `client_id == STATIC_CALLER_CLIENT_ID` delega en el resolutor de
    # puesto (`caller_id="owner"`); cualquier otro valor produce
    # `caller_id=f"oauth:{client_id}:{subject}"` -- lo que llega al
    # registro de auditoria de cada llamada (`dispatcher.py::_record`).
    assert oauth_result.client_id == client_id
    assert oauth_result.client_id != STATIC_CALLER_CLIENT_ID
    assert oauth_result.subject == str(owner_id)
