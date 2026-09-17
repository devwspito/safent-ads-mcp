"""`LagCurve` Kaplan-Meier (profitability-engine.md §2): cohortes sinteticas
-> `F(d)` conocido ±5%; las cohortes truncadas no sesgan la parte ya
observada de la curva; cohorte inmadura no propone subir."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from safent_ads.economics.domain.errors import NoConvergedCurveError
from safent_ads.economics.domain.lag_curve import (
    MIN_OBSERVATIONS_FOR_PHASE_CURVE,
    CohortProjection,
    LagCurve,
    LagObservation,
    resolve_median_lag_days,
)
from safent_ads.signals.domain.gates import AttributionLagGate


def _observations(
    converted_days: list[int], *, non_converters: int, d_max: int
) -> list[LagObservation]:
    obs = [LagObservation(duration_days=d, converted=True) for d in converted_days]
    obs += [LagObservation(duration_days=d_max, converted=False) for _ in range(non_converters)]
    return obs


class TestKaplanMeierMatchesEmpiricalCdf:
    """Sin censura previa a la conversion, KM coincide con la CDF empirica."""

    def test_f_matches_known_fractions(self) -> None:
        # 20 a los 1 dia, 30 a los 5, 50 a los 10 -> F(1)=0.20, F(5)=0.50, F(10)=1.0
        converted = [1] * 20 + [5] * 30 + [10] * 50
        curve = LagCurve.from_observations(_observations(converted, non_converters=0, d_max=30))

        assert curve.f(1) == pytest.approx(0.20, abs=0.05)
        assert curve.f(5) == pytest.approx(0.50, abs=0.05)
        assert curve.f(10) == pytest.approx(1.00, abs=0.05)


class TestTruncatedCohortsDoNotBiasCurve:
    def test_early_curve_invariant_to_later_censoring(self) -> None:
        full = [2] * 3 + [4] * 4 + [8] * 5 + [15] * 2 + [20] * 2 + [25] * 1
        non_converters = 83
        full_curve = LagCurve.from_observations(
            _observations(full, non_converters=non_converters, d_max=30)
        )

        # Vista truncada al dia 5: todo lo que ocurriria despues queda
        # censurado en 5, como si aun no lo supieramos.
        truncated = [LagObservation(d, True) if d <= 5 else LagObservation(5, False) for d in full]
        truncated += [LagObservation(5, False) for _ in range(non_converters)]
        truncated_curve = LagCurve.from_observations(truncated, d_max=30)

        assert truncated_curve.f(2) == pytest.approx(full_curve.f(2), abs=1e-9)
        assert truncated_curve.f(4) == pytest.approx(full_curve.f(4), abs=1e-9)


class TestMedianLagDays:
    def test_median_is_first_day_reaching_half_of_final_value(self) -> None:
        converted = [1] * 20 + [5] * 30 + [10] * 50
        curve = LagCurve.from_observations(_observations(converted, non_converters=0, d_max=30))
        assert curve.median_lag_days() == 5

    def test_no_conversions_raises(self) -> None:
        curve = LagCurve.from_observations(_observations([], non_converters=50, d_max=30))
        with pytest.raises(NoConvergedCurveError):
            curve.median_lag_days()


class TestCohortProjectionMaturityRule:
    def _mature_curve(self) -> LagCurve:
        converted = [1] * 20 + [5] * 30 + [10] * 50
        return LagCurve.from_observations(_observations(converted, non_converters=0, d_max=30))

    def test_immature_cohort_cannot_raise(self) -> None:
        """Cohorte inmadura (edad 1 dia, madurez baja) no propone subir."""
        curve = self._mature_curve()
        projection = CohortProjection.build(curve=curve, observed=5, age_days=1)
        assert projection.maturity < 0.60
        assert projection.can_raise is False

    def test_mature_cohort_can_raise(self) -> None:
        curve = self._mature_curve()
        projection = CohortProjection.build(curve=curve, observed=45, age_days=10)
        assert projection.maturity >= 0.60
        assert projection.can_raise is True

    def test_lower_requires_optimistic_scenario_still_bad(self) -> None:
        curve = self._mature_curve()
        projection = CohortProjection.build(curve=curve, observed=25, age_days=5)
        assert projection.maturity >= 0.30
        # Escenario optimista sigue peor que el objetivo -> se puede bajar.
        assert projection.can_lower(
            optimistic_cost_per_conversion=100.0, target_cost_per_conversion=50.0
        )
        # Escenario optimista ya cumple el objetivo -> no es seguro bajar.
        assert not projection.can_lower(
            optimistic_cost_per_conversion=40.0, target_cost_per_conversion=50.0
        )

    def test_projected_scales_inversely_with_maturity(self) -> None:
        curve = self._mature_curve()
        projection = CohortProjection.build(curve=curve, observed=10, age_days=1)
        assert projection.projected > projection.observed
        assert projection.projected_low <= projection.projected <= projection.projected_high


def _curve_with_sample(n_converted: int, *, d_max: int = 30) -> LagCurve:
    """`n_converted` conversiones repartidas 20/30/50 % en 1/5/10 dias
    (misma forma que `TestMedianLagDays`), escaladas al tamano pedido."""
    converted = [1] * (n_converted * 20 // 100) + [5] * (n_converted * 30 // 100)
    converted += [10] * (n_converted - len(converted))
    return LagCurve.from_observations(_observations(converted, non_converters=0, d_max=d_max))


class TestResolveMedianLagDays:
    """T157: el gate deja de leer una constante -- `resolve_median_lag_days`
    decide si hay curva suficiente para confiar en su mediana real."""

    def test_without_a_curve_falls_back_to_the_default(self) -> None:
        resolution = resolve_median_lag_days(None, default_median_lag_days=3)

        assert resolution.median_lag_days == 3
        assert resolution.is_calibrated is False
        assert resolution.sample_size == 0

    def test_below_the_minimum_sample_falls_back_even_with_a_real_curve(self) -> None:
        curve = _curve_with_sample(MIN_OBSERVATIONS_FOR_PHASE_CURVE - 1)

        resolution = resolve_median_lag_days(curve, default_median_lag_days=3)

        assert resolution.is_calibrated is False
        assert resolution.median_lag_days == 3
        assert resolution.sample_size == MIN_OBSERVATIONS_FOR_PHASE_CURVE - 1

    def test_with_enough_sample_uses_the_curves_real_median(self) -> None:
        curve = _curve_with_sample(MIN_OBSERVATIONS_FOR_PHASE_CURVE)

        resolution = resolve_median_lag_days(curve, default_median_lag_days=3)

        assert resolution.is_calibrated is True
        assert resolution.median_lag_days == curve.median_lag_days()
        assert resolution.sample_size == MIN_OBSERVATIONS_FOR_PHASE_CURVE

    def test_a_curve_that_never_converged_falls_back_too(self) -> None:
        curve = LagCurve.from_observations(
            _observations([], non_converters=MIN_OBSERVATIONS_FOR_PHASE_CURVE, d_max=30)
        )

        resolution = resolve_median_lag_days(curve, default_median_lag_days=3)

        assert resolution.is_calibrated is False
        assert resolution.median_lag_days == 3

    def test_immature_cohort_never_proposes_increase(self) -> None:
        """Aunque la puerta de rezago ya haya abierto (el dia de hoy supera
        el `median_lag_days` calibrado), la cohorte de esa misma ventana
        puede seguir inmadura: las dos senales tienen que estar de acuerdo
        para autorizar una subida, ninguna basta por si sola (profitability-
        engine.md §2: 'subir exige maturity >= 0.60')."""
        curve = _curve_with_sample(MIN_OBSERVATIONS_FOR_PHASE_CURVE)
        resolution = resolve_median_lag_days(curve, default_median_lag_days=3)
        assert resolution.is_calibrated is True

        window_end = date(2026, 1, 1)
        as_of = window_end + timedelta(days=resolution.median_lag_days)
        gate_verdict = AttributionLagGate.evaluate(
            window_end=window_end, median_lag_days=resolution.median_lag_days, as_of=as_of
        )
        assert gate_verdict.passed is True

        projection = CohortProjection.build(
            curve=curve, observed=5, age_days=resolution.median_lag_days
        )
        assert projection.maturity < 0.60
        assert projection.can_raise is False
