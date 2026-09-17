"""`IntrospectToken` (tasks.md T005): resuelve un token opaco presentado a
su concesion, para que la presentacion (`CompositeTokenVerifier`, T010)
decida si abre `/mcp`. Nunca lanza por token invalido: `active=False` es
la respuesta, igual que RFC 7662."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from safent_ads.mcp_oauth.application.ports import GrantRepository, TokenHasher
from safent_ads.mcp_oauth.domain.grant import TokenKind
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet
from safent_ads.shared.clock import Clock


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenIntrospection:
    active: bool
    client_id: str | None = None
    owner_id: uuid.UUID | None = None
    scope_set: ScopeSet | None = None
    resource: ResourceIndicator | None = None
    expires_at: datetime | None = None


_INACTIVE = TokenIntrospection(active=False)


class IntrospectToken:
    def __init__(self, *, grants: GrantRepository, token_hasher: TokenHasher, clock: Clock) -> None:
        self._grants = grants
        self._token_hasher = token_hasher
        self._clock = clock

    async def execute(self, raw_token: str) -> TokenIntrospection:
        token_hash = self._token_hasher.hash(raw_token)
        grant = await self._grants.get_by_token_hash(token_hash)
        if grant is None:
            return _INACTIVE
        now = self._clock.now()
        if not grant.is_token_valid(token_hash, TokenKind.ACCESS, now):
            return _INACTIVE
        token = grant.find_token(token_hash)
        assert token is not None  # noqa: S101 - is_token_valid ya confirmo que existe
        return TokenIntrospection(
            active=True,
            client_id=grant.client_id,
            owner_id=grant.owner_id,
            scope_set=grant.scope_set,
            resource=grant.resource,
            expires_at=token.expires_at,
        )
