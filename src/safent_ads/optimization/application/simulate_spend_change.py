"""`SimulateSpendChange` (contracts/mcp-tools.md P1 `simulate_spend_change`):
delta de contribucion, IC y `out_of_support` de un escenario "+X%"
(profitability-engine.md §7). Solo lectura -- no propone ni escribe."""

from __future__ import annotations

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.application.dto import SpendChangeSimulationView
from safent_ads.optimization.application.errors import (
    ContributionMarginNotFoundError,
    ResponseCurveNotFoundError,
)
from safent_ads.optimization.application.ports import (
    ContributionMarginPort,
    ResponseCurveRepository,
)
from safent_ads.optimization.domain.errors import OutOfSupportForecastError
from safent_ads.optimization.domain.response_curve import forecast_contribution_delta_band
from safent_ads.shared.ids import BusinessId, EntityRef


class SimulateSpendChange:
    def __init__(
        self, curves: ResponseCurveRepository, contribution_margins: ContributionMarginPort
    ) -> None:
        self._curves = curves
        self._contribution_margins = contribution_margins

    async def execute(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        product_id: str,
        current_daily_spend: Money,
        spend_multiplier: float,
    ) -> SpendChangeSimulationView:
        record = await self._curves.get_latest(business_id=business_id, entity_ref=entity_ref)
        if record is None:
            raise ResponseCurveNotFoundError(str(entity_ref))
        cm = await self._contribution_margins.get_contribution_margin_per_conversion(
            business_id=business_id, product_id=product_id
        )
        if cm is None:
            raise ContributionMarginNotFoundError(product_id)

        current_spend = float(current_daily_spend.amount)
        proposed_spend = current_spend * spend_multiplier
        try:
            band = forecast_contribution_delta_band(
                record.curve,
                current_spend=current_spend,
                spend_multiplier=spend_multiplier,
                contribution_margin_per_conversion=float(cm.amount),
                observed_range=record.observed_range,
                residual_std=record.residual_std,
                curve_confidence=record.curve_confidence,
            )
        except OutOfSupportForecastError:
            return SpendChangeSimulationView(
                entity_ref=str(entity_ref),
                current_spend=current_spend,
                proposed_spend=proposed_spend,
                out_of_support=True,
            )
        return SpendChangeSimulationView(
            entity_ref=str(entity_ref),
            current_spend=current_spend,
            proposed_spend=proposed_spend,
            out_of_support=False,
            expected_contribution_delta=Money.of(band.expected, cm.currency),
            low_contribution_delta=Money.of(band.low, cm.currency),
            high_contribution_delta=Money.of(band.high, cm.currency),
            band_confidence=band.band_confidence,
            curve_confidence=band.curve_confidence.value,
        )
