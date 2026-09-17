"""`ProposeReallocationPlan` (contracts/mcp-tools.md P1
`propose_reallocation_plan`): elige donante/receptor por `mContribution`,
construye el `AllocationPlan` equimarginal y lo entrega a `proposals` por el
puerto de salida -- **dos propuestas separadas**, bajada AUTO y subida con
aprobacion (FR-12). Propone, nunca escribe en plataforma."""

from __future__ import annotations

from safent_ads.optimization.application.dto import AllocationStepView, ReallocationPlanView
from safent_ads.optimization.application.errors import (
    NoReallocationCandidatesError,
    ReallocationVetoedError,
)
from safent_ads.optimization.application.ports import (
    ReallocationCandidateRepository,
    ReallocationProposalPort,
)
from safent_ads.optimization.domain.allocation import (
    AllocationStep,
    AllocationVetoReason,
    build_equimarginal_plan,
    select_donor_and_receiver,
)
from safent_ads.optimization.domain.identifiers import AllocationPlanId
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class ProposeReallocationPlan:
    def __init__(
        self,
        candidates: ReallocationCandidateRepository,
        proposals: ReallocationProposalPort,
        clock: Clock,
    ) -> None:
        self._candidates = candidates
        self._proposals = proposals
        self._clock = clock

    async def execute(self, *, business_id: BusinessId) -> ReallocationPlanView:
        pool = await self._candidates.list_candidates(business_id=business_id)
        pair = select_donor_and_receiver(pool)
        if pair is None:
            raise NoReallocationCandidatesError(str(business_id))
        donor, receiver = pair

        verdict = build_equimarginal_plan(
            plan_id=AllocationPlanId.new(),
            business_id=business_id,
            donor=donor,
            receiver=receiver,
            now=self._clock.now(),
        )
        if verdict.plan is None:
            raise ReallocationVetoedError(_veto_message(verdict.veto_reason))

        refs = await self._proposals.raise_reallocation_proposals(
            business_id=business_id, plan=verdict.plan
        )
        return ReallocationPlanView(
            donor=_step_view(verdict.plan.donor_step),
            receiver=_step_view(verdict.plan.receiver_step),
            expected_contribution_delta=verdict.plan.expected_contribution_delta,
            decrease_proposal_id=refs.decrease_proposal_id,
            increase_proposal_id=refs.increase_proposal_id,
        )


def _step_view(step: AllocationStep) -> AllocationStepView:
    return AllocationStepView(
        entity_ref=str(step.entity_ref),
        direction=step.direction.value,
        current_daily_spend=step.current_daily_spend,
        proposed_daily_spend=step.proposed_daily_spend,
        step_pct=step.step_pct,
        requires_approval=step.requires_approval,
    )


def _veto_message(reason: AllocationVetoReason | None) -> str:
    return reason.value if reason is not None else "unknown"
