"""`Grant`/`IssuedToken` (data-model.md, threat-model.md C-43): rotar deja
el token anterior ROTATED; usar un token ya ROTATED revoca la concesion
entera; el alcance nunca crece al refrescar."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.mcp_oauth.domain.errors import (
    GrantRevokedError,
    RefreshTokenReusedError,
    ScopeExpansionError,
    TokenExpiredError,
    UnknownTokenError,
)
from safent_ads.mcp_oauth.domain.grant import Grant, IssuedToken, TokenHash, TokenKind, TokenState
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet

_CREATED_AT = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_ACCESS_TTL = timedelta(minutes=60)
_REFRESH_TTL = timedelta(days=30)
_RESOURCE = ResourceIndicator("https://ads.example.com/mcp")


def _token(hash_seed: str, kind: TokenKind, *, issued_at: datetime, ttl: timedelta) -> IssuedToken:
    return IssuedToken(
        token_hash=TokenHash(hash_seed * 64),
        kind=kind,
        issued_at=issued_at,
        expires_at=issued_at + ttl,
    )


def _grant(access: IssuedToken, refresh: IssuedToken) -> Grant:
    return Grant(
        grant_id=uuid.uuid4(),
        authorization_request_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        client_id="client-1",
        scope_set=ScopeSet.parse("ads:read ads:propose"),
        resource=_RESOURCE,
        created_at=_CREATED_AT,
        tokens=(access, refresh),
    )


def test_rotate_marks_old_tokens_as_rotated() -> None:
    access = _token("1", TokenKind.ACCESS, issued_at=_CREATED_AT, ttl=_ACCESS_TTL)
    refresh = _token("2", TokenKind.REFRESH, issued_at=_CREATED_AT, ttl=_REFRESH_TTL)
    grant = _grant(access, refresh)
    now = _CREATED_AT + timedelta(minutes=61)
    new_access = _token("3", TokenKind.ACCESS, issued_at=now, ttl=_ACCESS_TTL)
    new_refresh = _token("4", TokenKind.REFRESH, issued_at=now, ttl=_REFRESH_TTL)

    grant.rotate_refresh_token(
        presented_hash=refresh.token_hash, new_access=new_access, new_refresh=new_refresh, now=now
    )

    assert access.state is TokenState.ROTATED
    assert refresh.state is TokenState.ROTATED
    assert grant.active_token(TokenKind.ACCESS) is new_access
    assert grant.active_token(TokenKind.REFRESH) is new_refresh
    assert new_refresh.rotated_from == refresh.token_hash


def test_using_a_rotated_refresh_token_revokes_the_whole_grant() -> None:
    access = _token("1", TokenKind.ACCESS, issued_at=_CREATED_AT, ttl=_ACCESS_TTL)
    refresh = _token("2", TokenKind.REFRESH, issued_at=_CREATED_AT, ttl=_REFRESH_TTL)
    grant = _grant(access, refresh)
    rotate_at = _CREATED_AT + timedelta(minutes=1)
    grant.rotate_refresh_token(
        presented_hash=refresh.token_hash,
        new_access=_token("3", TokenKind.ACCESS, issued_at=rotate_at, ttl=_ACCESS_TTL),
        new_refresh=_token("4", TokenKind.REFRESH, issued_at=rotate_at, ttl=_REFRESH_TTL),
        now=rotate_at,
    )

    with pytest.raises(RefreshTokenReusedError):
        grant.rotate_refresh_token(
            presented_hash=refresh.token_hash,
            new_access=_token("5", TokenKind.ACCESS, issued_at=rotate_at, ttl=_ACCESS_TTL),
            new_refresh=_token("6", TokenKind.REFRESH, issued_at=rotate_at, ttl=_REFRESH_TTL),
            now=rotate_at + timedelta(seconds=1),
        )

    assert grant.is_revoked is True
    assert grant.active_token(TokenKind.ACCESS) is None
    assert grant.active_token(TokenKind.REFRESH) is None


def test_rotating_an_already_revoked_grant_raises_grant_revoked() -> None:
    access = _token("1", TokenKind.ACCESS, issued_at=_CREATED_AT, ttl=_ACCESS_TTL)
    refresh = _token("2", TokenKind.REFRESH, issued_at=_CREATED_AT, ttl=_REFRESH_TTL)
    grant = _grant(access, refresh)
    grant.revoke(now=_CREATED_AT, reason="owner_requested")

    with pytest.raises(GrantRevokedError):
        grant.rotate_refresh_token(
            presented_hash=refresh.token_hash,
            new_access=_token("3", TokenKind.ACCESS, issued_at=_CREATED_AT, ttl=_ACCESS_TTL),
            new_refresh=_token("4", TokenKind.REFRESH, issued_at=_CREATED_AT, ttl=_REFRESH_TTL),
            now=_CREATED_AT,
        )


def test_rotating_with_an_expired_refresh_token_raises() -> None:
    access = _token("1", TokenKind.ACCESS, issued_at=_CREATED_AT, ttl=_ACCESS_TTL)
    refresh = _token("2", TokenKind.REFRESH, issued_at=_CREATED_AT, ttl=_REFRESH_TTL)
    grant = _grant(access, refresh)
    past_expiry = _CREATED_AT + _REFRESH_TTL + timedelta(seconds=1)

    with pytest.raises(TokenExpiredError):
        grant.rotate_refresh_token(
            presented_hash=refresh.token_hash,
            new_access=_token("3", TokenKind.ACCESS, issued_at=past_expiry, ttl=_ACCESS_TTL),
            new_refresh=_token("4", TokenKind.REFRESH, issued_at=past_expiry, ttl=_REFRESH_TTL),
            now=past_expiry,
        )


def test_rotating_with_an_unknown_hash_raises_unknown_token() -> None:
    access = _token("1", TokenKind.ACCESS, issued_at=_CREATED_AT, ttl=_ACCESS_TTL)
    refresh = _token("2", TokenKind.REFRESH, issued_at=_CREATED_AT, ttl=_REFRESH_TTL)
    grant = _grant(access, refresh)

    with pytest.raises(UnknownTokenError):
        grant.rotate_refresh_token(
            presented_hash=TokenHash("9" * 64),
            new_access=_token("3", TokenKind.ACCESS, issued_at=_CREATED_AT, ttl=_ACCESS_TTL),
            new_refresh=_token("4", TokenKind.REFRESH, issued_at=_CREATED_AT, ttl=_REFRESH_TTL),
            now=_CREATED_AT,
        )


def test_scope_narrowed_on_refresh_is_accepted() -> None:
    access = _token("1", TokenKind.ACCESS, issued_at=_CREATED_AT, ttl=_ACCESS_TTL)
    refresh = _token("2", TokenKind.REFRESH, issued_at=_CREATED_AT, ttl=_REFRESH_TTL)
    grant = _grant(access, refresh)

    grant.ensure_scope_within_grant(ScopeSet.parse("ads:read"))


def test_scope_cannot_grow_on_refresh() -> None:
    access = _token("1", TokenKind.ACCESS, issued_at=_CREATED_AT, ttl=_ACCESS_TTL)
    refresh = _token("2", TokenKind.REFRESH, issued_at=_CREATED_AT, ttl=_REFRESH_TTL)
    grant = Grant(
        grant_id=uuid.uuid4(),
        authorization_request_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        client_id="client-1",
        scope_set=ScopeSet.parse("ads:read"),
        resource=_RESOURCE,
        created_at=_CREATED_AT,
        tokens=(access, refresh),
    )

    with pytest.raises(ScopeExpansionError):
        grant.ensure_scope_within_grant(ScopeSet.parse("ads:read ads:propose"))
