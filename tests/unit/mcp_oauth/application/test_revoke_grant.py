"""`RevokeGrant` (contracts/oauth.md §6 `/revoke`, C-55 «Agentes
conectados»): revoca acceso+refresco; idempotente; opaco ante un
`grant_id` de otro propietario (mismo error que "no existe", anti-IDOR)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from safent_ads.mcp_oauth.application.errors import GrantNotFoundError
from safent_ads.mcp_oauth.application.revoke_grant import RevokeGrant
from safent_ads.mcp_oauth.domain.grant import Grant, IssuedToken, TokenHash, TokenKind
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.fakes import InMemoryGrantRepository
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _hash(seed: str) -> TokenHash:
    return TokenHash(seed * 64)


def _grant(owner_id: uuid.UUID) -> Grant:
    future = _NOW.replace(year=_NOW.year + 1)
    access = IssuedToken(
        token_hash=_hash("a"), kind=TokenKind.ACCESS, issued_at=_NOW, expires_at=future
    )
    refresh = IssuedToken(
        token_hash=_hash("b"), kind=TokenKind.REFRESH, issued_at=_NOW, expires_at=future
    )
    return Grant(
        grant_id=uuid.uuid4(),
        authorization_request_id=uuid.uuid4(),
        owner_id=owner_id,
        client_id="client-1",
        scope_set=ScopeSet.parse("ads:read"),
        resource=ResourceIndicator("https://ads.example.com/mcp"),
        created_at=_NOW,
        tokens=(access, refresh),
    )


async def test_revoke_marks_the_grant_and_its_tokens_as_revoked() -> None:
    owner_id = uuid.uuid4()
    grant = _grant(owner_id)
    grants = InMemoryGrantRepository()
    await grants.create(grant)
    use_case = RevokeGrant(grants=grants, clock=FixedClock(_NOW))

    await use_case.execute(grant_id=grant.id, owner_id=owner_id, reason="panel")

    assert grant.is_revoked is True
    assert grant.active_token(TokenKind.ACCESS) is None
    assert grant.active_token(TokenKind.REFRESH) is None
    assert grant.revoked_reason == "panel"


async def test_revoke_is_idempotent() -> None:
    owner_id = uuid.uuid4()
    grant = _grant(owner_id)
    grants = InMemoryGrantRepository()
    await grants.create(grant)
    use_case = RevokeGrant(grants=grants, clock=FixedClock(_NOW))
    await use_case.execute(grant_id=grant.id, owner_id=owner_id, reason="panel")

    await use_case.execute(grant_id=grant.id, owner_id=owner_id, reason="revoke_endpoint")

    assert grant.is_revoked is True
    assert grant.revoked_reason == "panel"


async def test_unknown_grant_raises() -> None:
    grants = InMemoryGrantRepository()
    use_case = RevokeGrant(grants=grants, clock=FixedClock(_NOW))

    with pytest.raises(GrantNotFoundError):
        await use_case.execute(grant_id=uuid.uuid4(), owner_id=uuid.uuid4(), reason="panel")


async def test_grant_of_another_owner_raises_not_found() -> None:
    grant = _grant(uuid.uuid4())
    grants = InMemoryGrantRepository()
    await grants.create(grant)
    use_case = RevokeGrant(grants=grants, clock=FixedClock(_NOW))

    with pytest.raises(GrantNotFoundError):
        await use_case.execute(grant_id=grant.id, owner_id=uuid.uuid4(), reason="panel")

    assert grant.is_revoked is False
