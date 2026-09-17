"""`PlatformCredential` (data-model.md `CredentialRef`, extendido para la
conexion OAuth desde la UI): metadatos de la credencial de una cuenta de
plataforma. **Nunca contiene el secreto** — solo el `CredentialRefId`/alias
que el broker resuelve contra su propio almacen cifrado
(threat-model.md C-24, `refs.py`: "Identidad opaca... nunca contiene el
secreto"). El token en si vive unicamente en
`broker/infrastructure/credential_store.py`."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from safent_ads.accounts.domain._aggregate import _EventRecordingAggregate
from safent_ads.accounts.domain.errors import InvalidStateTransitionError
from safent_ads.accounts.domain.events import CredentialInvalidated
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import BusinessId, PlatformCode


class CredentialStatus(StrEnum):
    CONNECTED = "connected"
    EXPIRED = "expired"
    REVOKED = "revoked"
    INVALID = "invalid"


_NEEDS_RECONNECT_STATES = frozenset(
    {CredentialStatus.EXPIRED, CredentialStatus.REVOKED, CredentialStatus.INVALID}
)


class CredentialHealth(StrEnum):
    """Salud observable de una credencial (threat-model.md C-21,
    `GET /platform-accounts` `token.health`): mas fina que `CredentialStatus`
    porque `EXPIRING_SOON` no es un estado persistido, es una ventana de
    tiempo sobre `expires_at` -- una credencial sigue `CONNECTED` en la
    base mientras se acerca su caducidad."""

    OK = "ok"
    EXPIRING_SOON = "expiring_soon"
    EXPIRED = "expired"
    REVOKED = "revoked"


_EXPIRING_SOON_WITHIN: Final[timedelta] = timedelta(days=7)

# `INVALID` (el broker no pudo revalidar el token en su cron) no tiene
# hueco propio en `tokenHealthSchema` (zod, panel): "expired" es el mas
# honesto de los disponibles -- exige reconectar, igual que un token
# caducado (checklists/panel-contract-followups.md #4).
_HEALTH_BY_TERMINAL_STATUS: Final[dict[CredentialStatus, CredentialHealth]] = {
    CredentialStatus.REVOKED: CredentialHealth.REVOKED,
    CredentialStatus.EXPIRED: CredentialHealth.EXPIRED,
    CredentialStatus.INVALID: CredentialHealth.EXPIRED,
}

_ERROR_CODE_BY_HEALTH: Final[dict[CredentialHealth, str]] = {
    CredentialHealth.EXPIRING_SOON: "TOKEN_EXPIRING_SOON",
    CredentialHealth.EXPIRED: "TOKEN_EXPIRED",
    CredentialHealth.REVOKED: "CREDENTIAL_REVOKED",
}


def classify_credential_health(
    status: CredentialStatus, expires_at: datetime | None, *, now: datetime
) -> CredentialHealth:
    """Regla unica detras de `token.health` (REST) y de las alertas de
    `CheckCredentialHealth` (threat-model.md C-21) -- un solo sitio que
    decide, nunca dos copias que puedan divergir."""
    mapped = _HEALTH_BY_TERMINAL_STATUS.get(status)
    if mapped is not None:
        return mapped
    if expires_at is None:
        return CredentialHealth.OK
    remaining = expires_at - now
    if remaining <= timedelta(0):
        return CredentialHealth.EXPIRED
    if remaining <= _EXPIRING_SOON_WITHIN:
        return CredentialHealth.EXPIRING_SOON
    return CredentialHealth.OK


def error_code_for_health(health: CredentialHealth) -> str | None:
    """`None` para `OK`: no hay nada que reportar como error todavia."""
    return _ERROR_CODE_BY_HEALTH.get(health)


@dataclass(slots=True)
class PlatformCredential(_EventRecordingAggregate):
    credential_ref_id: CredentialRefId
    business_id: BusinessId
    platform: PlatformCode
    alias: str
    scopes: frozenset[str]
    status: CredentialStatus = field(default=CredentialStatus.CONNECTED)
    obtained_at: datetime | None = field(default=None)
    expires_at: datetime | None = field(default=None)
    last_validated_at: datetime | None = field(default=None)
    revoked_at: datetime | None = field(default=None)
    checked_at: datetime | None = field(default=None)
    last_error_code: str | None = field(default=None)
    _pending_events: list[DomainEvent] = field(default_factory=list, repr=False)

    def needs_reconnect(self) -> bool:
        return self.status in _NEEDS_RECONNECT_STATES

    def health(self, *, now: datetime) -> CredentialHealth:
        return classify_credential_health(self.status, self.expires_at, now=now)

    def record_health_check(
        self,
        *,
        at: datetime,
        status: CredentialStatus,
        expires_at: datetime | None,
        error_code: str | None,
    ) -> None:
        """Resultado de un chequeo de salud (`CheckCredentialHealth`,
        threat-model.md C-21): `checked_at`/`last_error_code`/`expires_at`
        se mueven siempre; `status` solo si el chequeo aporta un estado
        nuevo, delegando en los metodos que ya protegen el invariante
        "REVOKED es terminal" (`revoke`/`mark_validated`)."""
        self.checked_at = at
        self.last_error_code = error_code
        if self.status == CredentialStatus.REVOKED:
            return
        self.expires_at = expires_at
        if status == CredentialStatus.REVOKED:
            self.revoke(at=at)
        elif status == CredentialStatus.CONNECTED:
            self.mark_validated(at=at, expires_at=expires_at)
        else:
            self.mark_invalid(at=at, reason=error_code or f"broker_status_{status.value}")

    def mark_validated(self, *, at: datetime, expires_at: datetime | None) -> None:
        """El cron de salud del broker (C-21) confirma que el token sigue
        siendo valido: limpia un `EXPIRED`/`INVALID` anterior si la
        plataforma vuelve a aceptarlo."""
        if self.status == CredentialStatus.REVOKED:
            raise InvalidStateTransitionError(
                "REVOKED -> CONNECTED no permitido: revocar es terminal"
            )
        self.status = CredentialStatus.CONNECTED
        self.last_validated_at = at
        self.expires_at = expires_at

    def mark_invalid(self, *, at: datetime, reason: str) -> None:
        if self.status == CredentialStatus.REVOKED:
            return
        self.status = CredentialStatus.INVALID
        self._record_event(
            CredentialInvalidated(
                business_id=self.business_id,
                occurred_at=at,
                credential_ref_id=str(self.credential_ref_id),
                platform=self.platform,
                reason=reason,
            )
        )

    def revoke(self, *, at: datetime) -> None:
        if self.status == CredentialStatus.REVOKED:
            raise InvalidStateTransitionError("credencial ya revocada")
        self.status = CredentialStatus.REVOKED
        self.revoked_at = at
        self._record_event(
            CredentialInvalidated(
                business_id=self.business_id,
                occurred_at=at,
                credential_ref_id=str(self.credential_ref_id),
                platform=self.platform,
                reason="revoked_by_owner",
            )
        )
