"""`Grant` + `IssuedToken` (data-model.md): la autoridad delegada viva.
Como mucho un token de acceso y uno de refresco ACTIVE por concesion;
rotar deja el anterior ROTATED (nunca se borra: hace falta para detectar
reuso, threat-model.md C-43); presentar un token que no este ACTIVE
revoca la concesion entera; el alcance nunca crece; el recurso es
inmutable."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.mcp_oauth.domain.errors import (
    GrantRevokedError,
    InvalidTokenHashError,
    RefreshTokenReusedError,
    ScopeExpansionError,
    TokenExpiredError,
    UnknownTokenError,
)
from safent_ads.mcp_oauth.domain.resource import ResourceIndicator
from safent_ads.mcp_oauth.domain.scope import ScopeSet

_HEX_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class TokenKind(StrEnum):
    ACCESS = "ACCESS"
    REFRESH = "REFRESH"


class TokenState(StrEnum):
    ACTIVE = "ACTIVE"
    ROTATED = "ROTATED"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class TokenHash:
    """sha256 hexadecimal de un token opaco. Solo esto se persiste
    (threat-model.md C-44): el valor en claro nunca llega a la base de
    datos."""

    value: str

    def __post_init__(self) -> None:
        if not _HEX_SHA256_PATTERN.match(self.value):
            raise InvalidTokenHashError(
                f"se esperaba sha256 hexadecimal de 64 caracteres: {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


class IssuedToken:
    def __init__(
        self,
        *,
        token_hash: TokenHash,
        kind: TokenKind,
        issued_at: datetime,
        expires_at: datetime,
        state: TokenState = TokenState.ACTIVE,
        rotated_from: TokenHash | None = None,
    ) -> None:
        self.token_hash = token_hash
        self.kind = kind
        self.issued_at = issued_at
        self.expires_at = expires_at
        self.state = state
        self.rotated_from = rotated_from

    def is_expired(self, now: datetime) -> bool:
        return now > self.expires_at

    def is_usable(self, now: datetime) -> bool:
        return self.state is TokenState.ACTIVE and not self.is_expired(now)


class Grant:
    def __init__(
        self,
        *,
        grant_id: uuid.UUID,
        authorization_request_id: uuid.UUID,
        owner_id: uuid.UUID,
        client_id: str,
        scope_set: ScopeSet,
        resource: ResourceIndicator,
        created_at: datetime,
        revoked_at: datetime | None = None,
        revoked_reason: str | None = None,
        tokens: tuple[IssuedToken, ...] = (),
    ) -> None:
        self.id = grant_id
        self.authorization_request_id = authorization_request_id
        self.owner_id = owner_id
        self.client_id = client_id
        self.scope_set = scope_set
        self.resource = resource
        self.created_at = created_at
        self.revoked_at = revoked_at
        self.revoked_reason = revoked_reason
        self._tokens: list[IssuedToken] = list(tokens)

    @property
    def tokens(self) -> tuple[IssuedToken, ...]:
        return tuple(self._tokens)

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def find_token(self, token_hash: TokenHash) -> IssuedToken | None:
        for token in self._tokens:
            if token.token_hash == token_hash:
                return token
        return None

    def active_token(self, kind: TokenKind) -> IssuedToken | None:
        for token in reversed(self._tokens):
            if token.kind is kind and token.state is TokenState.ACTIVE:
                return token
        return None

    def is_token_valid(self, token_hash: TokenHash, kind: TokenKind, now: datetime) -> bool:
        if self.is_revoked:
            return False
        token = self.find_token(token_hash)
        return token is not None and token.kind is kind and token.is_usable(now)

    def ensure_scope_within_grant(self, requested: ScopeSet) -> None:
        if not requested.is_subset_of(self.scope_set):
            raise ScopeExpansionError("el alcance solicitado excede la concesion original")

    def rotate_refresh_token(
        self,
        *,
        presented_hash: TokenHash,
        new_access: IssuedToken,
        new_refresh: IssuedToken,
        now: datetime,
    ) -> None:
        self._require_active()
        presented = self._require_refresh_token(presented_hash)
        if presented.state is not TokenState.ACTIVE:
            self.revoke(now=now, reason="refresh_token_reuse_detected")
            raise RefreshTokenReusedError(
                f"concesion {self.id} revocada por reuso de refresh token"
            )
        if presented.is_expired(now):
            raise TokenExpiredError(f"refresh token de la concesion {self.id} caducado")
        current_access = self.active_token(TokenKind.ACCESS)
        if current_access is not None:
            current_access.state = TokenState.ROTATED
        presented.state = TokenState.ROTATED
        # `rotated_from` solo tiene sentido en refresh (0035_mcp_oauth
        # `oauth_tokens_rotation_is_refresh_check`): la deteccion de reuso
        # (C-43) es sobre refresh tokens, no sobre el access que emiten.
        new_refresh.rotated_from = presented.token_hash
        self._tokens.extend((new_access, new_refresh))

    def revoke(self, *, now: datetime, reason: str) -> None:
        if self.is_revoked:
            return
        self.revoked_at = now
        self.revoked_reason = reason
        for token in self._tokens:
            if token.state is TokenState.ACTIVE:
                token.state = TokenState.REVOKED

    def _require_refresh_token(self, token_hash: TokenHash) -> IssuedToken:
        token = self.find_token(token_hash)
        if token is None or token.kind is not TokenKind.REFRESH:
            raise UnknownTokenError(f"refresh token desconocido en la concesion {self.id}")
        return token

    def _require_active(self) -> None:
        if self.is_revoked:
            raise GrantRevokedError(f"concesion {self.id} revocada")
