"""`GetTargetCpa` (contracts/mcp-tools.md P1 `get_target_cpa`). Separada de
`GetUnitEconomics` porque el consumidor tipico (una puerta o una regla) solo
necesita el objetivo, no la cascada completa de formulas."""

from __future__ import annotations

from safent_ads.economics.application.dto import TargetCpaView
from safent_ads.economics.application.errors import UnitEconomicsProfileNotFoundError
from safent_ads.economics.application.ports import UnitEconomicsProfileRepository
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.unit_economics import ProfileStatus
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class GetTargetCpa:
    def __init__(self, profiles: UnitEconomicsProfileRepository, clock: Clock) -> None:
        self._profiles = profiles
        self._clock = clock

    async def execute(self, *, business_id: BusinessId, product_id: ProductId) -> TargetCpaView:
        profile = await self._profiles.get_current(
            business_id=business_id, product_id=product_id, as_of=self._clock.now().date()
        )
        if profile is None:
            raise UnitEconomicsProfileNotFoundError(str(product_id))
        confidence = "confirmed" if profile.status is ProfileStatus.CONFIRMED else "provisional"
        return TargetCpaView(
            product_id=str(product_id),
            target_cost_per_conversion=profile.target_cost_per_conversion(),
            target_cost_per_lead=profile.target_cost_per_lead(),
            confidence=confidence,
        )
