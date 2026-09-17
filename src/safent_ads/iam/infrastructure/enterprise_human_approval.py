"""Server-configured EE origin only; consume once, never replay or renew consent."""

import json
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from safent_ads.iam.application.managed_ads_authority import ManagedAdsDenied, ManagedAdsUnavailable
from safent_ads.iam.application.managed_human_approval import (
    ManagedApprovalIntent,
    VerifiedManagedApproval,
)
from safent_ads.iam.infrastructure.enterprise_ads_authority import (
    EnterpriseAdsAuthority,
    _Principal,
    _unique_object,
)
from safent_ads.shared.managed_ads import ManagedAdsBinding

_MAX_ASSERTION_BYTES = 8192


class _Consumed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    active: bool
    principal: _Principal
    approved_by: str
    proposal_id: str
    diff_hash: str
    intent_id: str
    expires_at: int
    audience: str


class _Registered(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent_id: str
    proposal_id: str
    expires_at: str
    confirmation_required: bool


class EnterpriseHumanApproval(EnterpriseAdsAuthority):
    async def consume(
        self, assertion: str, *, binding: ManagedAdsBinding, proposal_id: UUID, diff_hash: str
    ) -> VerifiedManagedApproval:
        if (
            not isinstance(assertion, str)
            or not 1 <= len(assertion) <= _MAX_ASSERTION_BYTES
            or not assertion.isascii()
            or binding.org_id not in self._trust.allowed_org_ids
        ):
            raise ManagedAdsDenied("managed_human_approval_denied")
        raw = await self._request(
            self._trust.origin.rstrip("/") + "/internal/ads/human-approval/consume",
            {
                "assertion": assertion,
                "proposal_id": str(proposal_id),
                "diff_hash": diff_hash,
                "binding": binding.as_claims(),
            },
        )
        try:
            value = _Consumed.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
            intent_id = UUID(value.intent_id)
            if str(intent_id) != value.intent_id:
                raise ValueError
        except (ValueError, ValidationError, UnicodeError):
            raise ManagedAdsUnavailable("managed_human_invalid_response") from None
        if (
            not value.active
            or value.audience != "safent-ads-central"
            or value.principal.model_dump() != binding.as_claims()
            or value.approved_by != str(binding.user_id)
            or value.proposal_id != str(proposal_id)
            or value.diff_hash != diff_hash
            or not self._clock() < value.expires_at <= self._clock() + 120
        ):
            raise ManagedAdsDenied("managed_human_approval_denied")
        return VerifiedManagedApproval(
            binding, binding.user_id, proposal_id, diff_hash, intent_id, value.expires_at
        )

    async def register(self, snapshot: dict[str, object]) -> ManagedApprovalIntent:
        try:
            diff = snapshot["diff"]
            if not isinstance(diff, dict):
                raise ValueError
            binding = ManagedAdsBinding.from_claims(diff["managed_binding"])
            if binding.org_id not in self._trust.allowed_org_ids:
                raise ValueError
        except (ValueError, KeyError):
            raise ManagedAdsDenied("managed_human_approval_denied") from None
        raw = await self._request(
            self._trust.origin.rstrip("/") + "/internal/ads/human-approval/intents",
            {
                "registration_id": str(uuid4()),
                "snapshot": snapshot,
            },
        )
        try:
            value = _Registered.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
            intent_id, proposal_id = UUID(value.intent_id), UUID(value.proposal_id)
            expires_at = datetime.fromisoformat(value.expires_at).timestamp()
            if (
                not value.confirmation_required
                or str(intent_id) != value.intent_id
                or str(proposal_id) != value.proposal_id
                or value.proposal_id != snapshot["proposal_id"]
                or not self._clock() < expires_at <= self._clock() + 901
            ):
                raise ValueError
        except (ValueError, ValidationError, UnicodeError, KeyError):
            raise ManagedAdsUnavailable("managed_human_invalid_response") from None
        return ManagedApprovalIntent(intent_id, proposal_id, value.expires_at)
