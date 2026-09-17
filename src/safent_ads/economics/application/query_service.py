"""`EconomicsQueryService`: fachada de los 5 casos de uso de lectura sobre
un unico punto de inyeccion (patron `panel.application.ports.PanelReadPort`,
pero repartido en los puertos propios de `economics` en vez de uno
monolitico). MCP y REST (`presentation/`) dependen solo de esto -- ninguno
de los dos conoce `UnitEconomicsProfileRepository` ni los demas puertos."""

from __future__ import annotations

from safent_ads.economics.application.dto import (
    CohortProjectionView,
    LagCurveView,
    PlatformDivergenceView,
    TargetCpaView,
    UnitEconomicsView,
)
from safent_ads.economics.application.get_attribution_lag_curve import GetAttributionLagCurve
from safent_ads.economics.application.get_cohort_projection import GetCohortProjection
from safent_ads.economics.application.get_platform_divergence import GetPlatformDivergence
from safent_ads.economics.application.get_target_cpa import GetTargetCpa
from safent_ads.economics.application.get_unit_economics import GetUnitEconomics
from safent_ads.economics.application.ports import (
    LagCurveRepository,
    PlatformDivergenceRepository,
    UnitEconomicsProfileRepository,
)
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class EconomicsQueryService:
    def __init__(
        self,
        *,
        profiles: UnitEconomicsProfileRepository,
        lag_curves: LagCurveRepository,
        divergences: PlatformDivergenceRepository,
        clock: Clock,
    ) -> None:
        self._unit_economics = GetUnitEconomics(profiles, clock)
        self._target_cpa = GetTargetCpa(profiles, clock)
        self._lag_curve = GetAttributionLagCurve(lag_curves)
        self._cohort_projection = GetCohortProjection(lag_curves)
        self._platform_divergence = GetPlatformDivergence(divergences)

    async def unit_economics(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> UnitEconomicsView:
        return await self._unit_economics.execute(business_id=business_id, product_id=product_id)

    async def target_cpa(
        self, *, business_id: BusinessId, product_id: ProductId
    ) -> TargetCpaView:
        return await self._target_cpa.execute(business_id=business_id, product_id=product_id)

    async def lag_curve(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str
    ) -> LagCurveView:
        return await self._lag_curve.execute(
            business_id=business_id, product_id=product_id, platform=platform
        )

    async def cohort_projection(
        self,
        *,
        business_id: BusinessId,
        product_id: ProductId,
        platform: str,
        observed: int,
        age_days: int,
    ) -> CohortProjectionView:
        return await self._cohort_projection.execute(
            business_id=business_id,
            product_id=product_id,
            platform=platform,
            observed=observed,
            age_days=age_days,
        )

    async def platform_divergence(
        self, *, business_id: BusinessId, platform_account_id: str
    ) -> PlatformDivergenceView:
        return await self._platform_divergence.execute(
            business_id=business_id, platform_account_id=platform_account_id
        )
