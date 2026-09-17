"""A physical broker quota bucket, never an OAuth connection or credential."""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from safent_ads.shared.ids import PlatformCode


class LedgerScopeError(ValueError):
    """The broker cannot prove the quota identity of this request."""


class LegacyLedgerScopeError(LedgerScopeError):
    """Unattributed historical effects require explicit operator reconciliation."""


@dataclass(frozen=True, slots=True)
class LedgerScope:
    business_id: UUID
    platform: PlatformCode
    external_account_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.business_id, UUID) or not isinstance(self.platform, PlatformCode):
            raise LedgerScopeError("ledger_scope_invalid")
        if not self.external_account_id or not self.external_account_id.strip():
            raise LedgerScopeError("ledger_scope_invalid")

    @property
    def key(self) -> str:
        canonical = json.dumps(
            [str(self.business_id), self.platform.value, self.external_account_id],
            separators=(",", ":"),
        )
        return "physical:v1:" + hashlib.sha256(canonical.encode()).hexdigest()
