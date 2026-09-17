"""Separate proof of human consent; delegation capabilities never implement it."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from safent_ads.shared.managed_ads import ManagedAdsBinding


@dataclass(frozen=True, slots=True)
class VerifiedManagedApproval:
    binding: ManagedAdsBinding
    user_id: UUID
    proposal_id: UUID
    diff_hash: str
    intent_id: UUID
    expires_at: int


@dataclass(frozen=True, slots=True)
class ManagedApprovalIntent:
    intent_id: UUID
    proposal_id: UUID
    expires_at: str


class ManagedHumanApprovalPort(Protocol):
    async def consume(
        self, assertion: str, *, binding: ManagedAdsBinding, proposal_id: UUID, diff_hash: str
    ) -> VerifiedManagedApproval: ...


class ManagedApprovalRegistrationPort(Protocol):
    async def register(self, snapshot: dict[str, object]) -> ManagedApprovalIntent: ...
