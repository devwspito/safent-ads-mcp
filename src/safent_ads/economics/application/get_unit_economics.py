"""`GetUnitEconomics` (contracts/mcp-tools.md P1 `get_unit_economics`):
contribucion, objetivos, dato vs inferencia (`status`), `confidence`."""

from __future__ import annotations

from datetime import date

from safent_ads.economics.application.dto import UnitEconomicsView
from safent_ads.economics.application.errors import UnitEconomicsProfileNotFoundError
from safent_ads.economics.application.ports import UnitEconomicsProfileRepository
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class GetUnitEconomics:
    def __init__(self, profiles: UnitEconomicsProfileRepository, clock: Clock) -> None:
        self._profiles = profiles
        self._clock = clock

    async def execute(self, *, business_id: BusinessId, product_id: ProductId) -> UnitEconomicsView:
        profile = await self._profiles.get_current(
            business_id=business_id, product_id=product_id, as_of=self._as_of()
        )
        if profile is None:
            raise UnitEconomicsProfileNotFoundError(str(product_id))
        return UnitEconomicsView(
            product_id=str(product_id),
            version=profile.version,
            effective_from=profile.effective_from,
            status=profile.status.value,
            net_revenue=profile.net_revenue(),
            collected=profile.collected(),
            contribution_margin=profile.contribution_margin(),
            target_cost_per_conversion=profile.target_cost_per_conversion(),
            target_cost_per_lead=profile.target_cost_per_lead(),
            target_roas=profile.target_roas(),
            theta=float(profile.theta.value),
            margin_horizon_days=profile.margin_horizon_days,
        )

    def _as_of(self) -> date:
        return self._clock.now().date()
