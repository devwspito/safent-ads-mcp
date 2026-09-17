"""Bucle de calibracion (profitability-engine.md §6): n<20 no ajusta;
*property test* de monotonia conservadora -- guardarrail innegociable: en
`AUTO` la calibracion nunca se afloja."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import product

import pytest

from safent_ads.optimization.domain.calibration import (
    AutonomyLevel,
    CalibrationAdjustment,
    OutcomeSource,
    SignalOutcome,
    ThresholdDirection,
    assert_does_not_loosen_auto_rule,
    assert_sufficient_sample,
    build_aggressive_step,
    build_conservative_step,
    compute_precision,
    recommend_calibration,
)
from safent_ads.optimization.domain.errors import (
    CalibrationDirectionViolationError,
    InsufficientCalibrationSampleError,
)
from safent_ads.optimization.domain.identifiers import CalibrationAdjustmentId, SignalOutcomeId

_NOW = datetime(2026, 9, 9, tzinfo=UTC)
_BOTH_DIRECTIONS = (
    ThresholdDirection.HIGHER_IS_MORE_CONSERVATIVE,
    ThresholdDirection.LOWER_IS_MORE_CONSERVATIVE,
)


def _outcomes(n: int, *, correct: int) -> tuple[SignalOutcome, ...]:
    return tuple(
        SignalOutcome(
            outcome_id=SignalOutcomeId.new(),
            business_id="b1",
            account_id="a1",
            rule_code="M05",
            signal_id=f"s{i}",
            outcome_source=OutcomeSource.APPLIED,
            was_correct=i < correct,
            observed_at=_NOW,
        )
        for i in range(n)
    )


class TestMinimumSample:
    def test_below_20_only_shows_the_data(self) -> None:
        report = compute_precision(_outcomes(10, correct=8))
        assert report is not None
        recommendation = recommend_calibration(report)
        assert recommendation.should_tighten is False
        assert recommendation.should_loosen is False

    def test_assert_sufficient_sample_raises_below_20(self) -> None:
        with pytest.raises(InsufficientCalibrationSampleError):
            assert_sufficient_sample(19)
        assert_sufficient_sample(20)  # no lanza


class TestPrecisionRecommendation:
    def test_low_precision_recommends_tightening(self) -> None:
        report = compute_precision(_outcomes(20, correct=10))  # precision 0.50
        assert report is not None
        recommendation = recommend_calibration(report)
        assert recommendation.should_tighten is True

    def test_high_precision_with_enough_sample_recommends_loosening(self) -> None:
        report = compute_precision(_outcomes(40, correct=38))  # precision 0.95
        assert report is not None
        recommendation = recommend_calibration(report)
        assert recommendation.should_loosen is True

    def test_high_precision_below_loosen_sample_does_not_loosen(self) -> None:
        report = compute_precision(_outcomes(25, correct=24))  # precision 0.96, n<40
        assert report is not None
        recommendation = recommend_calibration(report)
        assert recommendation.should_loosen is False

    def test_expired_and_rejected_outcomes_count_as_control(self) -> None:
        expired = SignalOutcome(
            outcome_id=SignalOutcomeId.new(),
            business_id="b1",
            account_id="a1",
            rule_code="M05",
            signal_id="s-expired",
            outcome_source=OutcomeSource.EXPIRED,
            was_correct=False,
            observed_at=_NOW,
        )
        report = compute_precision((*_outcomes(19, correct=15), expired))
        assert report is not None
        assert report.sample_size == 20


class TestConservativeStepAlwaysAllowedForAuto:
    @pytest.mark.parametrize("direction", _BOTH_DIRECTIONS)
    def test_conservative_step_moves_towards_conservative(
        self, direction: ThresholdDirection
    ) -> None:
        adjustment = build_conservative_step(
            adjustment_id=CalibrationAdjustmentId.new(),
            rule_code="M05",
            threshold_name="cpa_multiple",
            direction=direction,
            current_value=1.5,
            threshold_range=(1.0, 3.0),
            now=_NOW,
        )
        assert adjustment.moved_towards_conservative is True
        assert_does_not_loosen_auto_rule(adjustment, autonomy_level=AutonomyLevel.AUTO)  # no lanza


class TestMonotonicConservativeInvariant:
    """*Property test*: para cualquier combinacion de direccion y valor
    inicial dentro del rango, un paso agresivo sobre una regla AUTO
    **siempre** se rechaza -- nunca se recorta en silencio."""

    @pytest.mark.parametrize(
        ("direction", "current_value"),
        list(product(_BOTH_DIRECTIONS, [1.2, 1.5, 1.8, 2.1, 2.4, 2.7])),
    )
    def test_aggressive_step_on_auto_rule_always_raises(
        self, direction: ThresholdDirection, current_value: float
    ) -> None:
        with pytest.raises(CalibrationDirectionViolationError):
            build_aggressive_step(
                adjustment_id=CalibrationAdjustmentId.new(),
                rule_code="M05",
                threshold_name="cpa_multiple",
                direction=direction,
                current_value=current_value,
                threshold_range=(1.0, 3.0),
                autonomy_level=AutonomyLevel.AUTO,
                now=_NOW,
            )

    @pytest.mark.parametrize("direction", _BOTH_DIRECTIONS)
    def test_aggressive_step_on_approval_rule_is_allowed(
        self, direction: ThresholdDirection
    ) -> None:
        adjustment = build_aggressive_step(
            adjustment_id=CalibrationAdjustmentId.new(),
            rule_code="M05",
            threshold_name="cpa_multiple",
            direction=direction,
            current_value=1.8,
            threshold_range=(1.0, 3.0),
            autonomy_level=AutonomyLevel.APPROVAL,
            now=_NOW,
        )
        assert adjustment.moved_towards_conservative is False

    def test_manually_constructed_loosening_adjustment_is_rejected_for_auto(self) -> None:
        # Construccion directa (bypass de los constructores "seguros"):
        # el guardarrail lo atrapa igualmente en el borde del agregado.
        adjustment = CalibrationAdjustment(
            adjustment_id=CalibrationAdjustmentId.new(),
            rule_code="M05",
            threshold_name="cpa_multiple",
            direction=ThresholdDirection.HIGHER_IS_MORE_CONSERVATIVE,
            previous_value=2.0,
            new_value=1.5,  # baja -> menos conservador con HIGHER_IS_MORE_CONSERVATIVE
            autonomy_level=AutonomyLevel.AUTO,
            created_at=_NOW,
        )
        with pytest.raises(CalibrationDirectionViolationError):
            assert_does_not_loosen_auto_rule(adjustment, autonomy_level=AutonomyLevel.AUTO)
