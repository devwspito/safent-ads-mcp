"""`IntrospectToken` (tasks.md T005): nunca lanza -- `active=False` para
token desconocido, caducado, revocado o de tipo `refresh` (RFC 7662)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from safent_ads.mcp_oauth.application.introspect_token import IntrospectToken
from safent_ads.mcp_oauth.domain.grant import Grant, IssuedToken, TokenKind
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.fakes import InMemoryGrantRepository
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_ACCESS_RAW = "the-access-token"  # noqa: S105 - fixture, no secreto real
_REFRESH_RAW = "the-refresh-token"  # noqa: S105 - fixture, no secreto real


async def _grant_with_tokens(
    grants: InMemoryGrantRepository,
    hasher: Sha256TokenHasher,
    *,
    owner_id: uuid.UUID,
    access_expires_at: datetime,
) -> Grant:
    access = IssuedToken(
        token_hash=hasher.hash(_ACCESS_RAW),
        kind=TokenKind.ACCESS,
        issued_at=_NOW,
        expires_at=access_expires_at,
    )
    refresh = IssuedToken(
        token_hash=hasher.hash(_REFRESH_RAW),
        kind=TokenKind.REFRESH,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
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
    await grants.create(grant)
    return grant


async def test_active_access_token_returns_its_grant_data() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    owner_id = uuid.uuid4()
    await _grant_with_tokens(
        grants, hasher, owner_id=owner_id, access_expires_at=_NOW + timedelta(hours=1)
    )
    use_case = IntrospectToken(grants=grants, token_hasher=hasher, clock=FixedClock(_NOW))

    result = await use_case.execute(_ACCESS_RAW)

    assert result.active is True
    assert result.client_id == "client-1"
    assert result.owner_id == owner_id
    assert result.scope_set == ScopeSet.parse("ads:read")
    assert result.resource == ResourceIndicator("https://ads.example.com/mcp")


async def test_unknown_token_is_inactive() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    use_case = IntrospectToken(grants=grants, token_hasher=hasher, clock=FixedClock(_NOW))

    result = await use_case.execute("never-issued")

    assert result.active is False


async def test_expired_access_token_is_inactive() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    await _grant_with_tokens(
        grants, hasher, owner_id=uuid.uuid4(), access_expires_at=_NOW - timedelta(seconds=1)
    )
    use_case = IntrospectToken(grants=grants, token_hasher=hasher, clock=FixedClock(_NOW))

    result = await use_case.execute(_ACCESS_RAW)

    assert result.active is False


async def test_refresh_token_presented_as_access_is_inactive() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    await _grant_with_tokens(
        grants, hasher, owner_id=uuid.uuid4(), access_expires_at=_NOW + timedelta(hours=1)
    )
    use_case = IntrospectToken(grants=grants, token_hasher=hasher, clock=FixedClock(_NOW))

    result = await use_case.execute(_REFRESH_RAW)

    assert result.active is False


async def test_revoked_grant_makes_its_access_token_inactive() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    grant = await _grant_with_tokens(
        grants, hasher, owner_id=uuid.uuid4(), access_expires_at=_NOW + timedelta(hours=1)
    )
    grant.revoke(now=_NOW, reason="owner_requested")
    use_case = IntrospectToken(grants=grants, token_hasher=hasher, clock=FixedClock(_NOW))

    result = await use_case.execute(_ACCESS_RAW)

    assert result.active is False
