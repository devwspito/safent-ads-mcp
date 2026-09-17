"""`ContributionMarginPort` real: vista minima sobre `economics` (ISP,
`optimization.application.ports.ContributionMarginPort` docstring) que
envuelve `SqlUnitEconomicsProfileRepository.get_current`
(`economics/infrastructure/sql_repositories.py`, 0016_economics) en vez de
volver a leer `unit_economics_profiles` a mano -- un solo lugar calcula
`contribution_margin()` (override o formula, `UnitEconomicsProfile`)."""

from __future__ import annotations

from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.money import Money
from safent_ads.economics.infrastructure.sql_repositories import SqlUnitEconomicsProfileRepository
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = ["SqlContributionMarginPort"]


class SqlContributionMarginPort:
    """`optimization.application.ports.ContributionMarginPort`."""

    def __init__(self, profiles: SqlUnitEconomicsProfileRepository, clock: Clock) -> None:
        self._profiles = profiles
        self._clock = clock

    async def get_contribution_margin_per_conversion(
        self, *, business_id: BusinessId, product_id: str
    ) -> Money | None:
        profile = await self._profiles.get_current(
            business_id=business_id,
            product_id=ProductId.parse(product_id),
            as_of=self._clock.now().date(),
        )
        return None if profile is None else profile.contribution_margin()
