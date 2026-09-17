"""`rule_evaluator`: Signal + Rule -> RuleOutcome segun autonomia
(tasks.md T038)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.rule_evaluator import (
    RuleOutcome,
    evaluate_creative_rule,
    evaluate_rule,
)
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from safent_ads.signals.domain.cause import Cause
from safent_ads.signals.domain.gate_verdict import GateName, GateVerdict
from safent_ads.signals.domain.signal import CreativeSignal, CreativeSignalKind, Signal, SignalKind
from safent_ads.signals.domain.value_objects import Evidence, MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan

_ENTITY = EntityRef(platform=PlatformCode.META, level=EntityLevel.AD_SET, external_id="e1")
_NOW = datetime(2026, 9, 9, tzinfo=UTC)
_GATE_VERDICTS = (GateVerdict.ok(GateName.LEARNING),)
_CONDITION = Condition(
    clauses=(
        ConditionClause(
            metric="roas",
            comparator=Comparator.LT,
            window="7d",
            threshold_kind=ThresholdKind.TARGET_RELATIVE_PCT,
            value=100,
        ),
    )
)


def _rule(**overrides: object) -> Rule:
    defaults: dict[str, object] = {
        "code": "M05",
        "platform": PlatformCode.META,
        "entity_level": EntityLevel.AD_SET,
        "description": "ROAS por debajo del objetivo",
        "condition": _CONDITION,
        "action_kind": ActionKind.SELL,
        "magnitude_pct": 30,
        "autonomy_level": AutonomyLevel.AUTO,
        "cooldown": timedelta(hours=24),
        "source_url": "https://bir.ch/facebook-automated-rules",
    }
    defaults.update(overrides)
    return Rule(**defaults)  # type: ignore[arg-type]


def _signal(kind: SignalKind, rule_code: str) -> Signal:
    return Signal(
        entity_ref=_ENTITY,
        kind=kind,
        strength=SignalStrength(80),
        cause=Cause.ROAS_BELOW_TARGET_SUSTAINED,
        cause_sentence="ROAS bajo objetivo",
        span=WindowSpan.D7,
        money_at_stake=MoneyAtStake(minor_units=10_000, currency="EUR"),
        evidence=Evidence(metric="roas", actual=1.0, target=2.0, baseline=None, span=WindowSpan.D7),
        gate_verdicts=_GATE_VERDICTS,
        emitted_at=_NOW,
        rule_code=rule_code,
    )


def _creative_signal(kind: CreativeSignalKind, rule_code: str) -> CreativeSignal:
    return CreativeSignal(
        entity_ref=_ENTITY,
        kind=kind,
        strength=SignalStrength(80),
        cause=Cause.HOOK_RATE_LOW,
        cause_sentence="hook rate bajo",
        span=WindowSpan.D7,
        money_at_stake=MoneyAtStake(minor_units=5_000, currency="EUR"),
        evidence=Evidence(
            metric="hook_rate", actual=0.1, target=0.2, baseline=None, span=WindowSpan.D7
        ),
        gate_verdicts=_GATE_VERDICTS,
        emitted_at=_NOW,
        rule_code=rule_code,
    )


def test_noop_when_rule_code_does_not_match_signal() -> None:
    outcome = evaluate_rule(_rule(code="M05"), _signal(SignalKind.SELL, rule_code="M06"))

    assert outcome is RuleOutcome.NOOP


def test_noop_when_signal_kind_does_not_match_action() -> None:
    outcome = evaluate_rule(
        _rule(code="M05", action_kind=ActionKind.SELL), _signal(SignalKind.HOLD, rule_code="M05")
    )

    assert outcome is RuleOutcome.NOOP


def test_auto_action_for_auto_rule() -> None:
    outcome = evaluate_rule(
        _rule(code="M05", action_kind=ActionKind.SELL, autonomy_level=AutonomyLevel.AUTO),
        _signal(SignalKind.SELL, rule_code="M05"),
    )

    assert outcome is RuleOutcome.AUTO_ACTION


def test_propose_for_approval_rule() -> None:
    outcome = evaluate_rule(
        _rule(
            code="G01",
            action_kind=ActionKind.BUY,
            autonomy_level=AutonomyLevel.APPROVAL,
        ),
        _signal(SignalKind.BUY, rule_code="G01"),
    )

    assert outcome is RuleOutcome.PROPOSE


def test_notify_for_notify_rule() -> None:
    outcome = evaluate_rule(
        _rule(code="G02", action_kind=ActionKind.HOLD_ALL, autonomy_level=AutonomyLevel.NOTIFY),
        _signal(SignalKind.HOLD, rule_code="G02"),
    )

    assert outcome is RuleOutcome.NOTIFY


@pytest.mark.parametrize(
    ("action", "kind", "expected"),
    [
        (ActionKind.CREATIVE_KILL, CreativeSignalKind.LOSER, RuleOutcome.AUTO_ACTION),
        (ActionKind.CREATIVE_SCALE, CreativeSignalKind.WINNER, RuleOutcome.PROPOSE),
    ],
)
def test_creative_rule_matches_expected_kind(
    action: ActionKind, kind: CreativeSignalKind, expected: RuleOutcome
) -> None:
    autonomy = AutonomyLevel.AUTO if expected is RuleOutcome.AUTO_ACTION else AutonomyLevel.APPROVAL
    rule = _rule(code="M21", action_kind=action, autonomy_level=autonomy)

    outcome = evaluate_creative_rule(rule, _creative_signal(kind, rule_code="M21"))

    assert outcome is expected


def test_creative_noop_on_kind_mismatch() -> None:
    rule = _rule(
        code="M21", action_kind=ActionKind.CREATIVE_KILL, autonomy_level=AutonomyLevel.AUTO
    )

    outcome = evaluate_creative_rule(
        rule, _creative_signal(CreativeSignalKind.WINNER, rule_code="M21")
    )

    assert outcome is RuleOutcome.NOOP
