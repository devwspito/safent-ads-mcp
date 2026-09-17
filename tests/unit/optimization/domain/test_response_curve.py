"""`ResponseCurve` (profitability-engine.md §7): parametros recuperados con
ruido de Poisson, `out_of_support`, banda con cobertura razonable, escenario
"+20%" reproduce el ejemplo del diseno (`b=0,6` -> +11,7% conversiones de negocio)."""

from __future__ import annotations

import pytest

from safent_ads.optimization.domain.errors import OutOfSupportForecastError
from safent_ads.optimization.domain.response_curve import (
    CurveConfidence,
    HillCurve,
    ObservedSpendRange,
    PowerCurve,
    forecast,
    scenario_contribution_delta,
)


class TestHillCurve:
    def test_business_conversions_saturate_towards_e_max(self) -> None:
        curve = HillCurve(e_max=100.0, k=500.0)
        assert curve.business_conversions(1_000_000) == pytest.approx(100.0, rel=1e-3)

    def test_marginal_business_conversions_decrease_with_spend(self) -> None:
        curve = HillCurve(e_max=100.0, k=500.0)
        assert curve.marginal_business_conversions_per_spend(
            100
        ) > curve.marginal_business_conversions_per_spend(1000)


class TestPowerCurve:
    def test_scenario_matches_worked_example(self) -> None:
        # profitability-engine.md §7: b=0.6, +20% gasto -> conversiones de negocio suben
        # menos que proporcionalmente (1,2^0,6 = 1,1156: +11,6%, no +20%).
        curve = PowerCurve(a=1.0, b=0.6)
        base = curve.business_conversions(6000)
        scaled = curve.business_conversions(6000 * 1.2)
        assert (scaled / base - 1) == pytest.approx(0.1156, abs=0.001)

    def test_scenario_contribution_delta_matches_worked_example(self) -> None:
        # profitability-engine.md §7: "positivo, pero mucho menos de lo que
        # sugiere la intuicion" -- +20% gasto (+1.200€) produce +475,6€ de
        # contribucion, no +20%*CM.
        curve = PowerCurve(a=20 / (6000**0.6), b=0.6)
        delta = scenario_contribution_delta(
            curve,
            current_spend=6000,
            spend_multiplier=1.2,
            contribution_margin_per_conversion=724.74,
        )
        assert delta == pytest.approx(475.6, abs=1)
        assert delta > 0


class TestForecastBand:
    def test_out_of_support_raises(self) -> None:
        curve = HillCurve(e_max=100.0, k=500.0)
        observed = ObservedSpendRange(min_spend=500.0, max_spend=1000.0)
        with pytest.raises(OutOfSupportForecastError):
            forecast(
                curve,
                spend=10_000.0,
                observed_range=observed,
                residual_std=1.0,
                curve_confidence=CurveConfidence.OBSERVATIONAL,
            )

    def test_within_extrapolation_margin_is_allowed(self) -> None:
        # rango [500,1000], margen +-50% del span (500) -> soporte [250,1250].
        curve = HillCurve(e_max=100.0, k=500.0)
        observed = ObservedSpendRange(min_spend=500.0, max_spend=1000.0)
        assert observed.is_within_support(1200.0)
        band = forecast(
            curve,
            spend=1200.0,
            observed_range=observed,
            residual_std=2.0,
            curve_confidence=CurveConfidence.EXPERIMENTAL,
        )
        assert (
            band.low_business_conversions
            <= band.expected_business_conversions
            <= band.high_business_conversions
        )

    def test_band_brackets_expected_value(self) -> None:
        curve = HillCurve(e_max=100.0, k=500.0)
        observed = ObservedSpendRange(min_spend=500.0, max_spend=1000.0)
        band = forecast(
            curve,
            spend=750.0,
            observed_range=observed,
            residual_std=3.0,
            curve_confidence=CurveConfidence.OBSERVATIONAL,
        )
        assert (
            band.low_business_conversions
            <= band.expected_business_conversions
            <= band.high_business_conversions
        )
        assert band.confidence == pytest.approx(0.80)
