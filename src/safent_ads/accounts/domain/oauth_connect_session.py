"""`OAuthConnectSession` (data-model.md `ReconnectSession`, adaptado a la
conexion inicial "Conectar" desde la UI en vez de la reconexion de una
cuenta ya existente): **nunca contiene `code`, `code_verifier`,
`client_secret` ni token** — solo `state_hash` (hasheado igual que el
token de sesion), un solo uso, TTL corto. El canje real lo hace el broker
(`broker/application/oauth_connect_flow.py`); `ads-api` solo observa el
desenlace (contracts/rest-api.md §Conexiones)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.accounts.domain.errors import InvalidStateTransitionError
from safent_ads.shared.ids import BusinessId, PlatformCode


class OAuthSessionStatus(StrEnum):
    WAITING = "waiting"
    OK = "ok"
    ERROR = "error"


@dataclass(slots=True)
class OAuthConnectSession:
    session_id: uuid.UUID
    business_id: BusinessId
    owner_id: uuid.UUID
    provider: PlatformCode
    state_hash: str
    expires_at: datetime
    status: OAuthSessionStatus = OAuthSessionStatus.WAITING
    error_code: str | None = None
    completed_at: datetime | None = None
    connection_id: uuid.UUID | None = None

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def expire(self, *, at: datetime) -> bool:
        """Resolve abandoned work without changing an already terminal outcome."""
        if self.status != OAuthSessionStatus.WAITING or not self.is_expired(at):
            return False
        self.status = OAuthSessionStatus.ERROR
        self.error_code = "OAUTH_SESSION_EXPIRED"
        self.completed_at = at
        return True

    def mark_ok(self, *, at: datetime) -> None:
        self._require_waiting(at)
        self.status = OAuthSessionStatus.OK
        self.completed_at = at

    def mark_error(self, *, error_code: str, at: datetime) -> None:
        self._require_waiting(at)
        self.status = OAuthSessionStatus.ERROR
        self.error_code = error_code
        self.completed_at = at

    def _require_waiting(self, at: datetime) -> None:
        if self.status != OAuthSessionStatus.WAITING:
            raise InvalidStateTransitionError(
                f"sesion OAuth {self.session_id} ya resuelta ({self.status}): un solo uso"
            )
        if self.is_expired(at):
            raise InvalidStateTransitionError(f"sesion OAuth {self.session_id} caducada")
