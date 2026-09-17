"""`RefreshGrant` (contracts/oauth.md §6, threat-model.md C-43): rota
access+refresh; el reuso de un refresh ya rotado revoca la concesion
entera; el alcance nunca crece; `client_id`/`resource` deben coincidir."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from safent_ads.mcp_oauth.application.errors import (
    ClientMismatchError,
    InvalidTargetError,
    UnknownRefreshTokenError,
)
from safent_ads.mcp_oauth.application.refresh_grant import RefreshGrant
from safent_ads.mcp_oauth.domain.errors import RefreshTokenReusedError, ScopeExpansionError
from safent_ads.mcp_oauth.domain.grant import Grant, IssuedToken, TokenKind, TokenState
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.fakes import InMemoryGrantRepository
from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_RESOURCE = "https://ads.example.com/mcp"
_REFRESH_RAW = "initial-refresh-token"  # noqa: S105 - fixture, no secreto real


async def _seeded_grant(
    grants: InMemoryGrantRepository, hasher: Sha256TokenHasher, *, scope: str = "ads:read"
) -> Grant:
    access = IssuedToken(
        token_hash=hasher.hash("initial-access-token"),
        kind=TokenKind.ACCESS,
        issued_at=_NOW,
        expires_at=_NOW.replace(year=_NOW.year + 1),
    )
    refresh = IssuedToken(
        token_hash=hasher.hash(_REFRESH_RAW),
        kind=TokenKind.REFRESH,
        issued_at=_NOW,
        expires_at=_NOW.replace(year=_NOW.year + 1),
    )
    grant = Grant(
        grant_id=uuid.uuid4(),
        authorization_request_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        client_id="client-1",
        scope_set=ScopeSet.parse(scope),
        resource=ResourceIndicator(_RESOURCE),
        created_at=_NOW,
        tokens=(access, refresh),
    )
    await grants.create(grant)
    return grant


def _use_case(grants: InMemoryGrantRepository, hasher: Sha256TokenHasher) -> RefreshGrant:
    return RefreshGrant(
        grants=grants,
        token_hasher=hasher,
        token_factory=SecretsOpaqueTokenFactory(),
        clock=FixedClock(_NOW),
    )


async def test_rotates_the_token_pair() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    grant = await _seeded_grant(grants, hasher)
    use_case = _use_case(grants, hasher)

    pair = await use_case.execute(
        refresh_token=_REFRESH_RAW, client_id="client-1", resource=_RESOURCE, requested_scope=None
    )

    assert pair.refresh_token != _REFRESH_RAW
    rotated = grant.find_token(hasher.hash(_REFRESH_RAW))
    assert rotated is not None
    assert rotated.state is TokenState.ROTATED
    new_active = grant.active_token(TokenKind.REFRESH)
    assert new_active is not None
    assert new_active.token_hash == hasher.hash(pair.refresh_token)


async def test_reusing_a_rotated_refresh_token_revokes_the_grant() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    grant = await _seeded_grant(grants, hasher)
    use_case = _use_case(grants, hasher)
    await use_case.execute(
        refresh_token=_REFRESH_RAW, client_id="client-1", resource=_RESOURCE, requested_scope=None
    )

    with pytest.raises(RefreshTokenReusedError):
        await use_case.execute(
            refresh_token=_REFRESH_RAW,
            client_id="client-1",
            resource=_RESOURCE,
            requested_scope=None,
        )

    assert grant.is_revoked is True


async def test_unknown_refresh_token_raises() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    use_case = _use_case(grants, hasher)

    with pytest.raises(UnknownRefreshTokenError):
        await use_case.execute(
            refresh_token="never-issued",  # noqa: S106 - fixture, no secreto real
            client_id="client-1",
            resource=_RESOURCE,
            requested_scope=None,
        )


async def test_wrong_client_id_raises() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    await _seeded_grant(grants, hasher)
    use_case = _use_case(grants, hasher)

    with pytest.raises(ClientMismatchError):
        await use_case.execute(
            refresh_token=_REFRESH_RAW,
            client_id="someone-else",
            resource=_RESOURCE,
            requested_scope=None,
        )


async def test_wrong_resource_raises_invalid_target() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    await _seeded_grant(grants, hasher)
    use_case = _use_case(grants, hasher)

    with pytest.raises(InvalidTargetError):
        await use_case.execute(
            refresh_token=_REFRESH_RAW,
            client_id="client-1",
            resource="https://other.example/mcp",
            requested_scope=None,
        )


async def test_requesting_a_subset_scope_still_returns_the_full_grant_scope() -> None:
    """L1 de la revision de seguridad (16-sep): el refresco ya no estrecha
    el alcance al `scope` solicitado -- siempre devuelve el de la
    concesion completa, aunque el cliente pida un subconjunto valido."""
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    await _seeded_grant(grants, hasher, scope="ads:read ads:propose")
    use_case = _use_case(grants, hasher)

    pair = await use_case.execute(
        refresh_token=_REFRESH_RAW,
        client_id="client-1",
        resource=_RESOURCE,
        requested_scope="ads:read",
    )

    assert str(pair.scope_set) == "ads:propose ads:read"


async def test_requesting_no_scope_returns_the_full_grant_scope() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    await _seeded_grant(grants, hasher, scope="ads:read ads:propose")
    use_case = _use_case(grants, hasher)

    pair = await use_case.execute(
        refresh_token=_REFRESH_RAW, client_id="client-1", resource=_RESOURCE, requested_scope=None
    )

    assert str(pair.scope_set) == "ads:propose ads:read"


async def test_scope_cannot_grow() -> None:
    hasher = Sha256TokenHasher()
    grants = InMemoryGrantRepository()
    await _seeded_grant(grants, hasher, scope="ads:read")
    use_case = _use_case(grants, hasher)

    with pytest.raises(ScopeExpansionError):
        await use_case.execute(
            refresh_token=_REFRESH_RAW,
            client_id="client-1",
            resource=_RESOURCE,
            requested_scope="ads:read ads:propose",
        )
