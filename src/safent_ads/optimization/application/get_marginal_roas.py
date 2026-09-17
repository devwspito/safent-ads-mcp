"""`GetMarginalRoas` (contracts/mcp-tools.md P1 `get_marginal_roas`): valor,
IC y metodo de la ultima estimacion materializada de una entidad."""

from __future__ import annotations

from safent_ads.optimization.application.dto import MarginalRoasView
from safent_ads.optimization.application.errors import MarginalEstimateNotFoundError
from safent_ads.optimization.application.ports import MarginalEstimateRepository
from safent_ads.shared.ids import BusinessId, EntityRef


class GetMarginalRoas:
    def __init__(self, estimates: MarginalEstimateRepository) -> None:
        self._estimates = estimates

    async def execute(self, *, business_id: BusinessId, entity_ref: EntityRef) -> MarginalRoasView:
        estimate = await self._estimates.get_latest(business_id=business_id, entity_ref=entity_ref)
        if estimate is None:
            raise MarginalEstimateNotFoundError(str(entity_ref))
        return MarginalRoasView(
            entity_ref=str(entity_ref),
            value=estimate.value,
            ci_low=estimate.ci_low,
            ci_high=estimate.ci_high,
            method=estimate.method.value,
            sample_size=estimate.sample_size,
            inconclusive=estimate.inconclusive,
        )
