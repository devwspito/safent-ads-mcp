"""Enterprise admission port, not proof of a human approval or an OAuth vault.

An admission belongs to the current request only. Never cache it, put the short
token in a queue, or convert its `approve` ceiling into owner authentication.
Queued work will need its exact binding covered by the human authorization
signature and a fresh Enterprise admission immediately before the broker effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.shared.managed_ads import (
    MANAGED_ADS_CAPABILITIES as MANAGED_ADS_CAPABILITIES,  # noqa: PLC0414 - public compatibility re-export
)
from safent_ads.shared.managed_ads import (
    ManagedAdsBinding as ManagedAdsBinding,  # noqa: PLC0414 - public compatibility re-export
)

ManagedAdsOperation = Literal["read", "propose", "approve", "execute"]


class ManagedAdsDenied(Exception):
    """No current authority. Contains no token, response body or remote detail."""


class ManagedAdsUnavailable(Exception):
    """Cannot establish authority now: deny, never fall back to a local owner."""


@dataclass(frozen=True, slots=True)
class ManagedAdsAdmission:
    binding: ManagedAdsBinding
    expires_at: int
    operation: ManagedAdsOperation


class ManagedAdsAuthorityPort(Protocol):
    async def introspect(
        self,
        grant_token: str,
        *,
        expected_instance_id: UUID,
        operation: ManagedAdsOperation,
        account: AccountRef,
    ) -> ManagedAdsAdmission: ...


class ManagedAdsBindingAuthorityPort(Protocol):
    """Fresh admission of an exact, already human-signed queued reference.

    No short token or pairing bearer is refreshed. Implementations must make
    a new decision per call; callers verify signature and account before it.
    """

    async def admit_binding(
        self,
        binding: ManagedAdsBinding,
        *,
        operation: Literal["execute"] = "execute",
    ) -> ManagedAdsBinding: ...
