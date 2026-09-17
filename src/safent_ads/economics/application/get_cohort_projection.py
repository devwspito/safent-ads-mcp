"""`GetCohortProjection` (contracts/mcp-tools.md P1 `get_cohort_projection`):
observado, proyectado, madurez, IC -- la pieza que evita bajar (o subir)
gasto sobre una cohorte que todavia no ha tenido tiempo de convertir."""

from __future__ import annotations

from safent_ads.economics.application.dto import CohortProjectionView
from safent_ads.economics.application.errors import LagCurveNotFoundError
from safent_ads.economics.application.ports import LagCurveRepository
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.lag_curve import CohortProjection
from safent_ads.shared.ids import BusinessId


class GetCohortProjection:
    def __init__(self, curves: LagCurveRepository) -> None:
        self._curves = curves

    async def execute(
        self,
        *,
        business_id: BusinessId,
        product_id: ProductId,
        platform: str,
        observed: int,
        age_days: int,
    ) -> CohortProjectionView:
        curve = await self._curves.get_current(
            business_id=business_id, product_id=product_id, platform=platform
        )
        if curve is None:
            raise LagCurveNotFoundError(f"{product_id}:{platform}")
        projection = CohortProjection.build(curve=curve, observed=observed, age_days=age_days)
        return CohortProjectionView(
            product_id=str(product_id),
            platform=platform,
            age_days=age_days,
            observed=observed,
            maturity=projection.maturity,
            projected=projection.projected,
            projected_low=projection.projected_low,
            projected_high=projection.projected_high,
            can_raise=projection.can_raise,
        )
