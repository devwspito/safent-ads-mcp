"""`PlatformAccount` (data-model.md): cuenta de una plataforma publicitaria
perteneciente a un `Business`. Estado `ACTIVE -> THROTTLED -> ACTIVE`,
`ACTIVE -> SUSPENDED` (terminal hasta reconciliacion manual explicita),
`ACTIVE -> READ_ONLY -> ACTIVE` (plataforma caida y recuperada,
contracts/platform-port.md: "Caida de una plataforma ⇒ status = READ_ONLY
... reintento con espera creciente")."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from safent_ads.accounts.domain._aggregate import _EventRecordingAggregate
from safent_ads.accounts.domain.errors import InvalidStateTransitionError
from safent_ads.accounts.domain.events import PlatformAccountSuspended, PlatformAccountThrottled
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import BusinessId


class PlatformAccountStatus(StrEnum):
    ACTIVE = "active"
    THROTTLED = "throttled"
    SUSPENDED = "suspended"
    READ_ONLY = "read_only"


class ApiTier(StrEnum):
    """Nivel de acceso a la API de la plataforma (research §3)."""

    GOOGLE_EXPLORER = "google_explorer"
    GOOGLE_BASIC = "google_basic"
    GOOGLE_STANDARD = "google_standard"
    META_LIMITED = "meta_limited"
    META_FULL = "meta_full"


_RECOVERABLE_STATES = frozenset({PlatformAccountStatus.THROTTLED, PlatformAccountStatus.READ_ONLY})


@dataclass(slots=True)
class PlatformAccount(_EventRecordingAggregate):
    business_id: BusinessId
    account_ref: AccountRef
    currency: str
    timezone: str
    api_tier: ApiTier
    credential_ref_id: CredentialRefId
    status: PlatformAccountStatus = field(default=PlatformAccountStatus.ACTIVE)
    last_synced_at: datetime | None = field(default=None)
    connection_owner_id: UUID | None = field(default=None)
    _pending_events: list[DomainEvent] = field(default_factory=list, repr=False)

    def mark_throttled(self, *, occurred_at: datetime) -> None:
        self._require_status(PlatformAccountStatus.ACTIVE, target="THROTTLED")
        self.status = PlatformAccountStatus.THROTTLED
        self._record_event(
            PlatformAccountThrottled(
                business_id=self.business_id, occurred_at=occurred_at, account_ref=self.account_ref
            )
        )

    def mark_read_only(self) -> None:
        self._require_status(PlatformAccountStatus.ACTIVE, target="READ_ONLY")
        self.status = PlatformAccountStatus.READ_ONLY

    def mark_active(self) -> None:
        """Recuperacion desde `THROTTLED` o `READ_ONLY`. `SUSPENDED` solo sale
        via `reconcile` (accion humana explicita, no automatica)."""
        if self.status not in _RECOVERABLE_STATES:
            raise InvalidStateTransitionError(
                f"{self.status} -> ACTIVE no permitido automaticamente"
            )
        self.status = PlatformAccountStatus.ACTIVE

    def suspend(self, *, occurred_at: datetime, reason: str) -> None:
        self._require_status(PlatformAccountStatus.ACTIVE, target="SUSPENDED")
        self.status = PlatformAccountStatus.SUSPENDED
        self._record_event(
            PlatformAccountSuspended(
                business_id=self.business_id,
                occurred_at=occurred_at,
                account_ref=self.account_ref,
                reason=reason,
            )
        )

    def reconcile(self) -> None:
        """Unica salida de `SUSPENDED`: reconciliacion manual explicita
        (data-model.md: "terminal hasta reconciliacion manual")."""
        self._require_status(PlatformAccountStatus.SUSPENDED, target="ACTIVE (reconcile)")
        self.status = PlatformAccountStatus.ACTIVE

    def record_synced(self, *, at: datetime) -> None:
        if self.status == PlatformAccountStatus.SUSPENDED:
            raise InvalidStateTransitionError("cuenta SUSPENDED no admite sincronizacion")
        self.last_synced_at = at

    def _require_status(self, expected: PlatformAccountStatus, *, target: str) -> None:
        if self.status != expected:
            raise InvalidStateTransitionError(f"{self.status} -> {target} no permitido")
