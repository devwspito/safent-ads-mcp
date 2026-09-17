"""`ListGrants` (enmienda T015b, threat-model.md C-55): agentes conectados
del propietario, con el nombre y host del cliente resueltos, y solo
concesiones vivas."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from safent_ads.mcp_oauth.application.list_grants import ListGrants
from safent_ads.mcp_oauth.domain.client import OAuthClient, RedirectUri, TokenEndpointAuthMethod
from safent_ads.mcp_oauth.domain.grant import Grant, IssuedToken, TokenHash, TokenKind
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.fakes import (
    InMemoryClientRepository,
    InMemoryGrantRepository,
)

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _client() -> OAuthClient:
    return OAuthClient(
        client_id="client-1",
        client_name="Claude Code",
        redirect_uris=(RedirectUri("http://127.0.0.1:54321/callback"),),
        token_endpoint_auth_method=TokenEndpointAuthMethod.NONE,
        client_secret_hash=None,
        grant_types=("authorization_code",),
        requested_scope=ScopeSet.parse("ads:read"),
        created_at=_NOW,
        last_seen_at=_NOW + timedelta(days=1),
    )


def _grant(owner_id: uuid.UUID, *, revoked: bool = False) -> Grant:
    refresh_expiry = _NOW + timedelta(days=30)
    access = IssuedToken(
        token_hash=TokenHash("a" * 64), kind=TokenKind.ACCESS, issued_at=_NOW, expires_at=_NOW
    )
    refresh = IssuedToken(
        token_hash=TokenHash("b" * 64),
        kind=TokenKind.REFRESH,
        issued_at=_NOW,
        expires_at=refresh_expiry,
    )
    grant = Grant(
        grant_id=uuid.uuid4(),
        authorization_request_id=uuid.uuid4(),
        owner_id=owner_id,
        client_id="client-1",
        scope_set=ScopeSet.parse("ads:read"),
        resource=ResourceIndicator("https://ads.example.com/mcp"),
        created_at=_NOW,
        tokens=(access, refresh),
    )
    if revoked:
        grant.revoke(now=_NOW, reason="owner_requested")
    return grant


async def test_lists_connected_grants_with_client_name_and_redirect_host() -> None:
    owner_id = uuid.uuid4()
    clients = InMemoryClientRepository([_client()])
    grants = InMemoryGrantRepository()
    grant = _grant(owner_id)
    await grants.create(grant)
    use_case = ListGrants(grants=grants, clients=clients)

    summaries = await use_case.execute(owner_id=owner_id)

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.grant_id == grant.id
    assert summary.client_name == "Claude Code"
    assert summary.redirect_host == "127.0.0.1:54321"
    assert str(summary.scopes) == "ads:read"
    assert summary.expires_at == _NOW + timedelta(days=30)
    assert summary.last_used_at == _NOW


async def test_revoked_grants_are_excluded() -> None:
    owner_id = uuid.uuid4()
    clients = InMemoryClientRepository([_client()])
    grants = InMemoryGrantRepository()
    await grants.create(_grant(owner_id, revoked=True))
    use_case = ListGrants(grants=grants, clients=clients)

    summaries = await use_case.execute(owner_id=owner_id)

    assert summaries == []


async def test_grants_of_other_owners_are_excluded() -> None:
    owner_id = uuid.uuid4()
    clients = InMemoryClientRepository([_client()])
    grants = InMemoryGrantRepository()
    await grants.create(_grant(uuid.uuid4()))
    use_case = ListGrants(grants=grants, clients=clients)

    summaries = await use_case.execute(owner_id=owner_id)

    assert summaries == []


async def test_falls_back_to_client_id_when_client_no_longer_exists() -> None:
    owner_id = uuid.uuid4()
    clients = InMemoryClientRepository()
    grants = InMemoryGrantRepository()
    grant = _grant(owner_id)
    await grants.create(grant)
    use_case = ListGrants(grants=grants, clients=clients)

    summaries = await use_case.execute(owner_id=owner_id)

    assert summaries[0].client_name == "client-1"
    assert summaries[0].redirect_host == ""
    assert summaries[0].last_used_at == _NOW


async def test_last_used_at_is_the_latest_access_token_issued_at() -> None:
    """Una rotacion (T006) anade un access token nuevo con `issued_at` mas
    reciente -- `last_used_at` sigue al ultimo, no al primero."""
    owner_id = uuid.uuid4()
    clients = InMemoryClientRepository([_client()])
    grants = InMemoryGrantRepository()
    grant = _grant(owner_id)
    rotated_access = IssuedToken(
        token_hash=TokenHash("c" * 64),
        kind=TokenKind.ACCESS,
        issued_at=_NOW + timedelta(hours=1),
        expires_at=_NOW + timedelta(hours=2),
    )
    grant._tokens.append(rotated_access)  # noqa: SLF001 - fixture, simula una rotacion ya persistida
    await grants.create(grant)
    use_case = ListGrants(grants=grants, clients=clients)

    summaries = await use_case.execute(owner_id=owner_id)

    assert summaries[0].last_used_at == _NOW + timedelta(hours=1)


async def test_last_used_at_is_none_without_any_access_token() -> None:
    owner_id = uuid.uuid4()
    clients = InMemoryClientRepository([_client()])
    grants = InMemoryGrantRepository()
    grant = Grant(
        grant_id=uuid.uuid4(),
        authorization_request_id=uuid.uuid4(),
        owner_id=owner_id,
        client_id="client-1",
        scope_set=ScopeSet.parse("ads:read"),
        resource=ResourceIndicator("https://ads.example.com/mcp"),
        created_at=_NOW,
    )
    await grants.create(grant)
    use_case = ListGrants(grants=grants, clients=clients)

    summaries = await use_case.execute(owner_id=owner_id)

    assert summaries[0].last_used_at is None
