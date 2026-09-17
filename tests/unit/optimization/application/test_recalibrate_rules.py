"""`RecalibrateRules` (profitability-engine.md §6, tasks.md T200): solo
aprieta reglas `AUTO`, nunca las afloja, y respeta el limite de un ajuste
por regla y semana."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from safent_ads.optimization.application.ports import RuleCalibrationState
from safent_ads.optimization.application.recalibrate_rules import RecalibrateRules
from safent_ads.optimization.domain.calibration import (
    AutonomyLevel,
    OutcomeSource,
    SignalOutcome,
    ThresholdDirection,
)
from safent_ads.optimization.domain.identifiers import SignalOutcomeId
from safent_ads.optimization.testing.in_memory_repositories import (
    InMemoryCalibrationAdjustmentLogPort,
    InMemoryCalibrationInputPort,
    InMemoryRuleCalibrationStatePort,
)
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 9, tzinfo=UTC)  # miercoles
_WEEK_START = date(2026, 9, 7)  # lunes de esa misma semana ISO


def _outcomes(n: int, *, correct: int, rule_code: str = "G01") -> list[SignalOutcome]:
    return [
        SignalOutcome(
            outcome_id=SignalOutcomeId.new(),
            business_id=f"biz-{i % 3}",
            account_id="acc-1",
            rule_code=rule_code,
            signal_id=f"sig-{i}",
            outcome_source=OutcomeSource.EXPIRED,
            was_correct=i < correct,
            observed_at=_NOW,
        )
        for i in range(n)
    ]


class _Harness:
    def __init__(self) -> None:
        self.inputs = InMemoryCalibrationInputPort()
        self.state = InMemoryRuleCalibrationStatePort()
        self.log = InMemoryCalibrationAdjustmentLogPort()
        self.use_case = RecalibrateRules(
            inputs=self.inputs, state=self.state, log=self.log, clock=FixedClock(_NOW)
        )


def _seed_auto_rule(h: _Harness, *, rule_code: str, magnitude_pct: float | None = 30.0) -> None:
    h.state.seed(
        state=RuleCalibrationState(
            rule_code=rule_code,
            autonomy_level=AutonomyLevel.AUTO,
            magnitude_pct=magnitude_pct,
            cooldown_minutes=60.0,
        )
    )


async def test_tightens_magnitude_pct_when_precision_is_low() -> None:
    h = _Harness()
    _seed_auto_rule(h, rule_code="G01", magnitude_pct=30.0)
    h.inputs.seed(rule_code="G01", outcomes=_outcomes(20, correct=10))  # precision 0.50 < 0.70

    adjustments = await h.use_case.execute()

    assert len(adjustments) == 1
    adjustment = adjustments[0]
    assert adjustment.threshold_name == "magnitude_pct"
    assert adjustment.direction is ThresholdDirection.LOWER_IS_MORE_CONSERVATIVE
    assert adjustment.new_value < 30.0
    assert h.log.recorded == [adjustment]


async def test_falls_back_to_cooldown_when_rule_has_no_magnitude() -> None:
    h = _Harness()
    _seed_auto_rule(h, rule_code="M09", magnitude_pct=None)
    h.inputs.seed(rule_code="M09", outcomes=_outcomes(20, correct=5))

    adjustments = await h.use_case.execute()

    assert adjustments[0].threshold_name == "cooldown_minutes"
    assert adjustments[0].direction is ThresholdDirection.HIGHER_IS_MORE_CONSERVATIVE
    assert adjustments[0].new_value > 60.0


async def test_never_loosens_an_auto_rule_even_with_high_precision() -> None:
    h = _Harness()
    _seed_auto_rule(h, rule_code="G01")
    h.inputs.seed(rule_code="G01", outcomes=_outcomes(50, correct=48))  # precision 0.96

    adjustments = await h.use_case.execute()

    assert adjustments == ()
    assert h.state.applied == []


async def test_below_minimum_sample_does_not_adjust() -> None:
    h = _Harness()
    _seed_auto_rule(h, rule_code="G01")
    h.inputs.seed(rule_code="G01", outcomes=_outcomes(10, correct=2))  # n<20

    adjustments = await h.use_case.execute()

    assert adjustments == ()


async def test_skips_a_rule_not_in_auto_autonomy() -> None:
    h = _Harness()
    h.state.seed(
        state=RuleCalibrationState(
            rule_code="M02",
            autonomy_level=AutonomyLevel.APPROVAL,
            magnitude_pct=30.0,
            cooldown_minutes=60.0,
        )
    )
    h.inputs.seed(rule_code="M02", outcomes=_outcomes(20, correct=5))

    adjustments = await h.use_case.execute()

    assert adjustments == ()


async def test_at_most_one_adjustment_per_rule_per_week() -> None:
    h = _Harness()
    _seed_auto_rule(h, rule_code="G01")
    h.inputs.seed(rule_code="G01", outcomes=_outcomes(20, correct=5))
    h.log.mark_done(rule_code="G01", threshold_name="magnitude_pct", week_start=_WEEK_START)

    adjustments = await h.use_case.execute()

    assert adjustments == ()
    assert h.state.applied == []


async def test_records_an_adjustment_per_business_that_contributed_an_outcome() -> None:
    h = _Harness()
    _seed_auto_rule(h, rule_code="G01")
    h.inputs.seed(rule_code="G01", outcomes=_outcomes(21, correct=5))  # biz-0/1/2

    await h.use_case.execute()

    assert len(h.log.recorded) == 1


@pytest.mark.parametrize("rule_code", ["G01", "M09"])
async def test_conservative_step_never_moves_the_wrong_direction(rule_code: str) -> None:
    """*Property test* de monotonia conservadora a nivel de aplicacion:
    para cualquier regla AUTO con precision baja, el ajuste aplicado nunca
    se mueve en direccion agresiva (delega en `CalibrationAdjustment.
    moved_towards_conservative`, ya probado en el dominio; esto prueba que
    `RecalibrateRules` nunca construye el caso contrario)."""
    h = _Harness()
    _seed_auto_rule(h, rule_code=rule_code, magnitude_pct=30.0 if rule_code == "G01" else None)
    h.inputs.seed(rule_code=rule_code, outcomes=_outcomes(20, correct=0))

    adjustments = await h.use_case.execute()

    assert adjustments[0].moved_towards_conservative
