"""Prepare a stored proposal, never a browser-authored replacement payload."""

from safent_ads.iam.application.managed_ads_authority import ManagedAdsAdmission, ManagedAdsDenied
from safent_ads.iam.application.managed_human_approval import (
    ManagedApprovalIntent,
    ManagedApprovalRegistrationPort,
)
from safent_ads.proposals.application.ports import ProposalRepository
from safent_ads.proposals.domain.diff_hash import compute_diff_hash, to_jsonable
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.proposal import ProposalState
from safent_ads.shared.clock import Clock


class PrepareManagedApproval:
    def __init__(
        self,
        proposals: ProposalRepository,
        authority: ManagedApprovalRegistrationPort,
        clock: Clock,
    ) -> None:
        self._proposals, self._authority, self._clock = proposals, authority, clock

    async def execute(
        self, proposal_id: ProposalId, admission: ManagedAdsAdmission
    ) -> ManagedApprovalIntent:
        proposal = await self._proposals.get(proposal_id)
        if (
            admission.operation != "approve"
            or self._clock.now().timestamp() >= admission.expires_at
            or proposal is None
            or proposal.state != ProposalState.PENDING
            or proposal.is_expired(self._clock.now())
            or proposal.diff.managed_binding != admission.binding
        ):
            raise ManagedAdsDenied("managed_human_approval_denied")
        diff = proposal.diff
        if (
            compute_diff_hash(
                diff.entity_ref, diff.parameter, diff.before, diff.after, diff.managed_binding
            )
            != diff.diff_hash
        ):
            raise ManagedAdsDenied("managed_human_approval_denied")
        return await self._authority.register(
            {
                "proposal_id": str(proposal_id),
                "diff_hash": diff.diff_hash,
                "diff": {
                    "entity_ref": str(diff.entity_ref),
                    "parameter": diff.parameter,
                    "before": to_jsonable(diff.before),
                    "after": to_jsonable(diff.after),
                    "managed_binding": admission.binding.as_claims(),
                },
            }
        )
