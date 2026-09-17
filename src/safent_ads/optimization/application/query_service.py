"""`OptimizationQueryService`: fachada de los casos de uso de `optimization`
sobre un unico punto de inyeccion (mismo patron que
`economics.application.query_service.EconomicsQueryService`). MCP y REST
dependen solo de esto."""

from __future__ import annotations

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.application.diagnose_entity import DiagnoseEntity
from safent_ads.optimization.application.dto import (
    DiagnosisView,
    MarginalRoasView,
    ReallocationPlanView,
    SpendChangeSimulationView,
)
from safent_ads.optimization.application.get_marginal_roas import GetMarginalRoas
from safent_ads.optimization.application.ports import (
    ContributionMarginPort,
    DiagnosisMetricsPort,
    MarginalEstimateRepository,
    ReallocationCandidateRepository,
    ReallocationProposalPort,
    ResponseCurveRepository,
)
from safent_ads.optimization.application.propose_reallocation_plan import ProposeReallocationPlan
from safent_ads.optimization.application.simulate_spend_change import SimulateSpendChange
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityRef


class OptimizationQueryService:
    def __init__(
        self,
        *,
        marginal_estimates: MarginalEstimateRepository,
        diagnosis_metrics: DiagnosisMetricsPort,
        response_curves: ResponseCurveRepository,
        contribution_margins: ContributionMarginPort,
        reallocation_candidates: ReallocationCandidateRepository,
        reallocation_proposals: ReallocationProposalPort,
        clock: Clock,
    ) -> None:
        self._marginal_roas = GetMarginalRoas(marginal_estimates)
        self._diagnose = DiagnoseEntity(diagnosis_metrics)
        self._simulate = SimulateSpendChange(response_curves, contribution_margins)
        self._propose_reallocation = ProposeReallocationPlan(
            reallocation_candidates, reallocation_proposals, clock
        )

    async def marginal_roas(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> MarginalRoasView:
        return await self._marginal_roas.execute(business_id=business_id, entity_ref=entity_ref)

    async def diagnose_entity(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> DiagnosisView:
        return await self._diagnose.execute(business_id=business_id, entity_ref=entity_ref)

    async def simulate_spend_change(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        product_id: str,
        current_daily_spend: Money,
        spend_multiplier: float,
    ) -> SpendChangeSimulationView:
        return await self._simulate.execute(
            business_id=business_id,
            entity_ref=entity_ref,
            product_id=product_id,
            current_daily_spend=current_daily_spend,
            spend_multiplier=spend_multiplier,
        )

    async def propose_reallocation_plan(self, *, business_id: BusinessId) -> ReallocationPlanView:
        return await self._propose_reallocation.execute(business_id=business_id)
