"""Bloques estadisticos puros (profitability-engine.md §4): valores de
referencia contra tablas normales estandar publicadas."""

from __future__ import annotations

import pytest

from safent_ads.economics.domain.statistics import (
    inverse_normal_cdf,
    poisson_confidence_interval,
    sample_size_per_arm,
)


class TestInverseNormalCdf:
    @pytest.mark.parametrize(
        ("probability", "expected"),
        [
            (0.975, 1.959964),
            (0.95, 1.644854),
            (0.90, 1.281552),
            (0.80, 0.841621),
            (0.50, 0.0),
        ],
    )
    def test_known_quantiles(self, probability: float, expected: float) -> None:
        assert inverse_normal_cdf(probability) == pytest.approx(expected, abs=1e-5)

    def test_symmetry(self) -> None:
        assert inverse_normal_cdf(0.05) == pytest.approx(-inverse_normal_cdf(0.95), abs=1e-9)

    def test_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="probability"):
            inverse_normal_cdf(1.0)


class TestSampleSizePerArm:
    def test_business_conversion_over_lead_matches_worked_example(self) -> None:
        """profitability-engine.md §4: conversion de negocio sobre lead, p=4.5%,
        MDE 20% -> 6.560 leads/brazo (inviable con volumenes tipicos del negocio)."""
        n = sample_size_per_arm(baseline_rate=0.045, relative_mde=0.20)
        assert n == pytest.approx(6560, abs=5)

    def test_lead_over_click_matches_worked_example(self) -> None:
        """profitability-engine.md §4: lead sobre clic, p=8%, MDE 25% ->
        2.274 clics/brazo (alcanzable en 2-3 semanas)."""
        n = sample_size_per_arm(baseline_rate=0.08, relative_mde=0.25)
        assert n == pytest.approx(2274, abs=5)

    def test_monotonic_in_mde(self) -> None:
        """Un MDE mas pequeno exige mas muestra."""
        big_mde = sample_size_per_arm(baseline_rate=0.05, relative_mde=0.30)
        small_mde = sample_size_per_arm(baseline_rate=0.05, relative_mde=0.10)
        assert small_mde > big_mde

    def test_rejects_invalid_baseline(self) -> None:
        with pytest.raises(ValueError, match="baseline_rate"):
            sample_size_per_arm(baseline_rate=1.5, relative_mde=0.10)


class TestPoissonConfidenceInterval:
    def test_zero_count_lower_bound_is_zero(self) -> None:
        low, _high = poisson_confidence_interval(0, 0.90)
        assert low == 0.0

    def test_interval_contains_count(self) -> None:
        low, high = poisson_confidence_interval(20, 0.90)
        assert low < 20 < high

    def test_wider_confidence_widens_interval(self) -> None:
        low_90, high_90 = poisson_confidence_interval(20, 0.90)
        low_95, high_95 = poisson_confidence_interval(20, 0.95)
        assert low_95 <= low_90
        assert high_95 >= high_90

    def test_rejects_negative_count(self) -> None:
        with pytest.raises(ValueError, match="count"):
            poisson_confidence_interval(-1, 0.90)
