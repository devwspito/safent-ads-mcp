"""`ComputeLagCurve` (T156, profitability-engine.md §2): recalcula
`LagCurve` (Kaplan-Meier) para `(business_id, product_id, platform)` desde
las observaciones crudas y la materializa (`LagCurveRepository.save`,
UPSERT -- idempotente: recalcular con los mismos datos produce la misma
curva)."""

from __future__ import annotations

from safent_ads.economics.application.ports import LagCurveRepository, LagObservationRepository
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.lag_curve import DEFAULT_D_MAX, LagCurve
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class ComputeLagCurve:
    def __init__(
        self,
        observations: LagObservationRepository,
        curves: LagCurveRepository,
        clock: Clock,
        *,
        d_max: int = DEFAULT_D_MAX,
    ) -> None:
        self._observations = observations
        self._curves = curves
        self._clock = clock
        self._d_max = d_max

    async def execute(
        self, *, business_id: BusinessId, product_id: ProductId, platform: str
    ) -> LagCurve | None:
        as_of = self._clock.now().date()
        observations = await self._observations.fetch_observations(
            business_id=business_id, product_id=product_id, platform=platform, as_of=as_of
        )
        if not observations:
            return None
        curve = LagCurve.from_observations(observations, d_max=self._d_max)
        await self._curves.save(
            business_id=business_id, product_id=product_id, platform=platform, curve=curve
        )
        return curve
