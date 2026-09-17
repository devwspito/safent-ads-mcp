"""`GetAttributionLagCurve` (contracts/mcp-tools.md P1
`get_attribution_lag_curve`): expone `F(d)` materializada, no la recalcula
en cada llamada (`LagCurveRepository` es un snapshot, `MaintenanceCycle` lo
refresca)."""

from __future__ import annotations

from safent_ads.economics.application.dto import LagCurveView
from safent_ads.economics.application.errors import LagCurveNotFoundError
from safent_ads.economics.application.ports import LagCurveRepository
from safent_ads.economics.domain.errors import NoConvergedCurveError
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.shared.ids import BusinessId

_SAMPLE_EVERY_N_DAYS = 5


class GetAttributionLagCurve:
    def __init__(self, curves: LagCurveRepository) -> None:
        self._curves = curves

    async def execute(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str
    ) -> LagCurveView:
        curve = await self._curves.get_current(
            business_id=business_id, product_id=product_id, platform=platform
        )
        if curve is None:
            raise LagCurveNotFoundError(f"{product_id}:{platform}")
        try:
            median = curve.median_lag_days()
        except NoConvergedCurveError:
            median = None
        sampled = tuple(
            (day, curve.f(day)) for day in range(0, curve.d_max + 1, _SAMPLE_EVERY_N_DAYS)
        )
        return LagCurveView(
            product_id=str(product_id),
            platform=platform,
            d_max=curve.d_max,
            sample_size=curve.sample_size,
            median_lag_days=median,
            curve=sampled,
        )
