"""Emision de tokens opacos (access/refresh), compartida por `RedeemCode`
y `RefreshGrant` para que la generacion no se duplique (mismo motivo que
`iam/application/session_issuance.py`: superficie de seguridad, un solo
sitio)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from safent_ads.mcp_oauth.application.policy import ACCESS_TOKEN_TTL, REFRESH_TOKEN_TTL
from safent_ads.mcp_oauth.application.ports import OpaqueTokenFactory, TokenHasher
from safent_ads.mcp_oauth.domain.grant import IssuedToken, TokenKind


@dataclass(frozen=True, slots=True, kw_only=True)
class MintedToken:
    issued: IssuedToken
    raw_value: str


def mint_token(
    *,
    kind: TokenKind,
    ttl: timedelta,
    now: datetime,
    factory: OpaqueTokenFactory,
    hasher: TokenHasher,
) -> MintedToken:
    raw_value = factory.new_token()
    issued = IssuedToken(
        token_hash=hasher.hash(raw_value), kind=kind, issued_at=now, expires_at=now + ttl
    )
    return MintedToken(issued=issued, raw_value=raw_value)


def mint_access_and_refresh(
    *, now: datetime, factory: OpaqueTokenFactory, hasher: TokenHasher
) -> tuple[MintedToken, MintedToken]:
    access = mint_token(
        kind=TokenKind.ACCESS, ttl=ACCESS_TOKEN_TTL, now=now, factory=factory, hasher=hasher
    )
    refresh = mint_token(
        kind=TokenKind.REFRESH, ttl=REFRESH_TOKEN_TTL, now=now, factory=factory, hasher=hasher
    )
    return access, refresh
