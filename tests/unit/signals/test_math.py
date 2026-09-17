"""`anomaly.py` y `pacing.py`: formulas del catalogo con expectativas
calculadas a mano (tasks.md T036, rule-catalog-and-signals.md §3)."""

from __future__ import annotations

import math

import pytest

from safent_ads.signals.domain.anomaly import (
    AnomalySeverity,
    classify_z_severity,
    ewma,
    ewma_drift,
    same_weekday_z,
    wow_delta,
)
from safent_ads.signals.domain.errors import ZeroBaselineError
from safent_ads.signals.domain.pacing import (
    GOOGLE_DAILY_OVERDELIVERY_MULTIPLIER,
    GOOGLE_MONTHLY_MULTIPLIER,
    google_max_daily_charge_minor,
    google_monthly_equivalent_minor,
    new_daily_budget_even,
    new_daily_budget_weighted,
    pace_index,
    projected_spend,
    runway_days,
)

# --- same_weekday_z --------------------------------------------------------
# Ocho lunes: [100, 100, 100, 100, 100, 100, 100, 200].
# media = 112.5; varianza = mean((v-112.5)^2) = (7*12.5^2 + 87.5^2)/8
#       = (1093.75 + 7656.25)/8 = 1093.75; sigma = sqrt(1093.75) ~= 33.0681
# hoy = 250 -> z = (250-112.5)/33.0681 ~= 4.1583


def test_same_weekday_z_hand_computed() -> None:
    same_weekday_values = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 200.0]

    z = same_weekday_z(250.0, same_weekday_values)

    assert z == pytest.approx(4.1583, abs=1e-3)


def test_same_weekday_z_zero_variance_and_equal_to_mean_is_zero() -> None:
    assert same_weekday_z(100.0, [100.0] * 8) == 0.0


def test_same_weekday_z_zero_variance_and_above_mean_is_positive_infinity() -> None:
    assert same_weekday_z(150.0, [100.0] * 8) == math.inf


def test_same_weekday_z_zero_variance_and_below_mean_is_negative_infinity() -> None:
    assert same_weekday_z(50.0, [100.0] * 8) == -math.inf


# --- ewma / ewma_drift ------------------------------------------------------
# alpha(n=7) = 2/8 = 0.25; serie [100, 120]: S0=100, S1 = 0.25*120+0.75*100 = 105.


def test_ewma_hand_computed_two_points() -> None:
    assert ewma([100.0, 120.0], 7) == pytest.approx(105.0)


def test_ewma_drift_hand_computed() -> None:
    # 29 observaciones constantes salvo la ultima que sube: la rapida (n=7)
    # reacciona mas que la lenta (n=28) -> drift positivo.
    values = [100.0] * 28 + [200.0]

    drift = ewma_drift(values)

    fast = ewma(values, 7)
    slow = ewma(values, 28)
    assert drift == pytest.approx(fast / slow - 1)
    assert drift > 0


def test_ewma_drift_raises_on_zero_slow_baseline() -> None:
    with pytest.raises(ZeroBaselineError):
        ewma_drift([0.0] * 29)


# --- wow_delta ---------------------------------------------------------------


def test_wow_delta_hand_computed() -> None:
    assert wow_delta(120.0, 100.0) == pytest.approx(0.20)


def test_wow_delta_raises_on_zero_baseline() -> None:
    with pytest.raises(ZeroBaselineError):
        wow_delta(120.0, 0.0)


# --- pacing -------------------------------------------------------------------


def test_pace_index_hand_computed() -> None:
    # cap mensual 30.000, 10 dias de 30 transcurridos -> esperado = 10.000.
    # actual 11.000 -> pace_index = 1.10 (110%).
    index = pace_index(
        actual_mtd_minor=11_000, monthly_cap_minor=30_000, days_elapsed=10, days_in_month=30
    )

    assert index == pytest.approx(1.10)


def test_projected_spend_hand_computed() -> None:
    # 11.000 en 10 dias -> 1.100/dia * 30 = 33.000.
    projected = projected_spend(actual_mtd_minor=11_000, days_elapsed=10, days_in_month=30)

    assert projected == pytest.approx(33_000.0)


def test_runway_days_hand_computed() -> None:
    assert runway_days(remaining_minor=5_000, avg_daily_minor=500.0) == pytest.approx(10.0)


def test_runway_days_is_infinite_with_zero_average_spend() -> None:
    assert runway_days(remaining_minor=5_000, avg_daily_minor=0.0) == math.inf


def test_new_daily_budget_even_hand_computed() -> None:
    assert new_daily_budget_even(remaining_minor=9_000, days_left=3) == pytest.approx(3_000.0)


def test_new_daily_budget_weighted_hand_computed() -> None:
    # remaining / (2*days_left - 1) = 9000 / 5 = 1800.
    assert new_daily_budget_weighted(remaining_minor=9_000, days_left=3) == pytest.approx(1_800.0)


def test_google_max_daily_charge_is_double() -> None:
    assert google_max_daily_charge_minor(1_000) == 2_000
    assert GOOGLE_DAILY_OVERDELIVERY_MULTIPLIER == 2.0


def test_google_monthly_equivalent_is_30_4x() -> None:
    assert google_monthly_equivalent_minor(1_000) == 30_400
    assert GOOGLE_MONTHLY_MULTIPLIER == 30.4


# --- classify_z_severity ------------------------------------------------------


@pytest.mark.parametrize(
    ("z", "expected"),
    [
        (1.9, AnomalySeverity.NONE),
        (-1.9, AnomalySeverity.NONE),
        (2.0, AnomalySeverity.WARN),
        (2.9, AnomalySeverity.WARN),
        (-2.9, AnomalySeverity.WARN),
        (3.0, AnomalySeverity.PAGE),
        (5.0, AnomalySeverity.PAGE),
    ],
)
def test_classify_z_severity_hand_computed_bands(z: float, expected: AnomalySeverity) -> None:
    assert classify_z_severity(z) == expected
