"""`estimate_marginal_contribution_paired` (profitability-engine.md §3a):
recupera `b*E/S` +-15% sobre un sintetico `E = a*S^b`; descarta pares
contaminados; `inconclusive` cuando el IC cruza el punto de equilibrio 1,0."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from safent_ads.optimization.domain.marginal import (
    DailyPair,
    EstimationMethod,
    VetoReason,
    estimate_marginal_contribution_paired,
)

_A = 10.0
_B = 0.6
_CM = Decimal("100")


def _synthetic_pairs(*, spend_a: float, spend_b: float) -> tuple[DailyPair, ...]:
    return tuple(
        DailyPair(
            spend_a=Decimal(str(spend_a)),
            spend_b=Decimal(str(spend_b)),
            conversions_a=_A * spend_a**_B,
            conversions_b=_A * spend_b**_B,
        )
        for _ in range(7)
    )


class TestSyntheticRecovery:
    def test_recovers_marginal_slope_within_15_percent(self) -> None:
        spend_a, spend_b = 700.0, 1050.0
        pairs = _synthetic_pairs(spend_a=spend_a, spend_b=spend_b)
        verdict = estimate_marginal_contribution_paired(
            pairs=pairs,
            contribution_margin_per_conversion=_CM,
            has_structural_change=False,
            rng=random.Random(42),
        )
        assert verdict.is_usable
        assert verdict.estimate is not None

        # Pendiente teorica en el punto medio del tramo, en unidades de
        # mContribution (CM * conversiones/euro).
        midpoint = (spend_a + spend_b) / 2
        theoretical_slope = _B * (_A * midpoint**_B) / midpoint
        expected = float(_CM) * theoretical_slope
        assert verdict.estimate.value == pytest.approx(expected, rel=0.15)
        assert verdict.estimate.method is EstimationMethod.PAIRED

    def test_bootstrap_confidence_interval_brackets_point_estimate(self) -> None:
        pairs = _synthetic_pairs(spend_a=700.0, spend_b=1050.0)
        verdict = estimate_marginal_contribution_paired(
            pairs=pairs,
            contribution_margin_per_conversion=_CM,
            has_structural_change=False,
            rng=random.Random(7),
        )
        assert verdict.estimate is not None
        assert verdict.estimate.ci_low <= verdict.estimate.value <= verdict.estimate.ci_high


class TestVetoes:
    def test_structural_change_is_vetoed(self) -> None:
        pairs = _synthetic_pairs(spend_a=700.0, spend_b=1050.0)
        verdict = estimate_marginal_contribution_paired(
            pairs=pairs, contribution_margin_per_conversion=_CM, has_structural_change=True
        )
        assert not verdict.is_usable
        assert verdict.veto_reason is VetoReason.STRUCTURAL_CHANGE

    def test_insufficient_spend_variation_is_vetoed(self) -> None:
        # |dS|/S_A = 50/700 ~ 7% < 15%: sin senal marginal.
        pairs = _synthetic_pairs(spend_a=700.0, spend_b=750.0)
        verdict = estimate_marginal_contribution_paired(
            pairs=pairs, contribution_margin_per_conversion=_CM, has_structural_change=False
        )
        assert not verdict.is_usable
        assert verdict.veto_reason is VetoReason.INSUFFICIENT_SPEND_VARIATION

    def test_wrong_pair_count_raises(self) -> None:
        pairs = _synthetic_pairs(spend_a=700.0, spend_b=1050.0)[:6]
        with pytest.raises(Exception, match="7 pares"):
            estimate_marginal_contribution_paired(
                pairs=pairs, contribution_margin_per_conversion=_CM, has_structural_change=False
            )


class TestInconclusive:
    def test_ci_crossing_breakeven_is_inconclusive(self) -> None:
        # Dias heterogeneos (5 "buenos", 2 "planos"): el bootstrap produce
        # replicas por debajo y por encima del punto de equilibrio 1,0.
        good_days = tuple(
            DailyPair(
                spend_a=Decimal("100"), spend_b=Decimal("140"), conversions_a=1.0, conversions_b=1.9
            )
            for _ in range(5)
        )
        flat_days = tuple(
            DailyPair(
                spend_a=Decimal("100"), spend_b=Decimal("140"), conversions_a=1.0, conversions_b=1.0
            )
            for _ in range(2)
        )
        verdict = estimate_marginal_contribution_paired(
            pairs=good_days + flat_days,
            contribution_margin_per_conversion=Decimal("50"),
            has_structural_change=False,
            rng=random.Random(1),
        )
        assert verdict.estimate is not None
        assert verdict.estimate.inconclusive is True

    def test_clearly_destructive_estimate_is_not_inconclusive(self) -> None:
        pairs = tuple(
            DailyPair(
                spend_a=Decimal("700"),
                spend_b=Decimal("1050"),
                conversions_a=2.10,
                conversions_b=2.15,
            )
            for _ in range(7)
        )
        verdict = estimate_marginal_contribution_paired(
            pairs=pairs,
            contribution_margin_per_conversion=Decimal("724.74"),
            has_structural_change=False,
            rng=random.Random(3),
        )
        assert verdict.estimate is not None
        assert verdict.estimate.destroys_contribution is True
        assert verdict.estimate.inconclusive is False
