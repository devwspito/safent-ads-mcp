"""`CompositeTokenVerifier` (tasks.md T010, T010+; threat-model.md C-48,
C-53): token OAuth valido -> `AccessToken` con recurso canonico; token
estatico -> `AccessToken` sintetico; token desconocido -> `None`; token
OAuth caducado -> `None`; el estatico se apaga sin tocar la rama OAuth."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from safent_ads.mcp_oauth.domain.grant import Grant, IssuedToken, TokenKind
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.mcp_oauth.infrastructure.fakes import FakeOAuthSession
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.mcp_oauth.presentation.token_verifier import (
    STATIC_CALLER_CLIENT_ID,
    CompositeTokenVerifier,
)
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_RESOURCE = "https://ads.example.com/mcp"
_STATIC_TOKEN = "the-static-token"  # noqa: S105 - fixture, no secreto real
_ACCESS_RAW = "the-oauth-access-token"  # noqa: S105 - fixture, no secreto real


async def _seed_active_grant(session: FakeOAuthSession, *, expires_at: datetime) -> uuid.UUID:
    hasher = Sha256TokenHasher()
    owner_id = uuid.uuid4()
    access = IssuedToken(
        token_hash=hasher.hash(_ACCESS_RAW), kind=TokenKind.ACCESS, issued_at=_NOW,
        expires_at=expires_at,
    )
    grant = Grant(
        grant_id=uuid.uuid4(),
        authorization_request_id=uuid.uuid4(),
        owner_id=owner_id,
        client_id="claude-code",
        scope_set=ScopeSet.parse("ads:read"),
        resource=ResourceIndicator(_RESOURCE),
        created_at=_NOW,
        tokens=(access,),
    )
    await session.grants.create(grant)
    return owner_id


def _verifier(
    *, oauth_enabled: bool = True, static_token: str | None = _STATIC_TOKEN
) -> tuple[CompositeTokenVerifier, FakeOAuthSession]:
    session = FakeOAuthSession()
    verifier = CompositeTokenVerifier(
        session_factory=(lambda: session) if oauth_enabled else None,
        token_hasher=Sha256TokenHasher(),
        clock=FixedClock(_NOW),
        static_token=static_token,
        resource=_RESOURCE,
    )
    return verifier, session


async def test_valid_oauth_token_returns_access_token_with_canonical_resource() -> None:
    verifier, session = _verifier()
    owner_id = await _seed_active_grant(session, expires_at=_NOW + timedelta(minutes=60))

    access = await verifier.verify_token(_ACCESS_RAW)

    assert access is not None
    assert access.client_id == "claude-code"
    assert access.resource == _RESOURCE
    assert access.subject == str(owner_id)
    assert access.scopes == ["ads:read"]


async def test_static_token_returns_synthetic_access_token() -> None:
    verifier, _ = _verifier()

    access = await verifier.verify_token(_STATIC_TOKEN)

    assert access is not None
    assert access.client_id == STATIC_CALLER_CLIENT_ID
    assert set(access.scopes) == {"ads:read", "ads:propose"}
    assert access.expires_at is None
    assert access.resource == _RESOURCE


async def test_unknown_token_returns_none() -> None:
    verifier, _ = _verifier()

    assert await verifier.verify_token("never-issued") is None


async def test_expired_oauth_token_returns_none() -> None:
    verifier, session = _verifier()
    await _seed_active_grant(session, expires_at=_NOW - timedelta(seconds=1))

    assert await verifier.verify_token(_ACCESS_RAW) is None


async def test_static_token_can_be_disabled() -> None:
    verifier, _ = _verifier(static_token=None)

    assert await verifier.verify_token(_STATIC_TOKEN) is None


async def test_oauth_disabled_still_allows_the_static_token() -> None:
    verifier, _ = _verifier(oauth_enabled=False)

    access = await verifier.verify_token(_STATIC_TOKEN)

    assert access is not None
    assert access.client_id == STATIC_CALLER_CLIENT_ID


async def test_wrong_static_token_of_the_same_length_is_rejected() -> None:
    verifier, _ = _verifier()

    wrong = "x" * len(_STATIC_TOKEN)

    assert await verifier.verify_token(wrong) is None
