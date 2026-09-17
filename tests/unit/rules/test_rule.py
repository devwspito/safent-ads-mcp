"""`Rule`: invariante FR-11/FR-12, una `AUTO` nunca sube gasto."""

from __future__ import annotations

from datetime import timedelta

import pytest

from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind
from safent_ads.rules.domain.errors import (
    BlankRuleCodeError,
    InvalidCooldownError,
    SpendIncreasingAutoRuleError,
)
from safent_ads.rules.domain.rule import Rule
from safent_ads.shared.ids import EntityLevel, PlatformCode

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


def test_auto_defensive_rule_is_valid() -> None:
    rule = _rule()

    assert rule.autonomy_level is AutonomyLevel.AUTO


@pytest.mark.parametrize(
    "action_kind",
    [ActionKind.BUY, ActionKind.CREATIVE_SCALE, ActionKind.LOOSEN_TARGET],
)
def test_rejects_auto_rule_that_increases_spend(action_kind: ActionKind) -> None:
    with pytest.raises(SpendIncreasingAutoRuleError):
        _rule(code="G01", action_kind=action_kind, autonomy_level=AutonomyLevel.AUTO)


def test_allows_approval_rule_that_increases_spend() -> None:
    rule = _rule(code="G01", action_kind=ActionKind.BUY, autonomy_level=AutonomyLevel.APPROVAL)

    assert rule.autonomy_level is AutonomyLevel.APPROVAL


def test_unpause_is_exempt_from_the_spend_increase_check() -> None:
    rule = _rule(
        code="M11",
        action_kind=ActionKind.UNPAUSE,
        autonomy_level=AutonomyLevel.AUTO,
        magnitude_pct=None,
    )

    assert rule.action_kind is ActionKind.UNPAUSE


def test_rejects_blank_code() -> None:
    with pytest.raises(BlankRuleCodeError):
        _rule(code="  ")


def test_rejects_negative_cooldown() -> None:
    with pytest.raises(InvalidCooldownError):
        _rule(cooldown=timedelta(hours=-1))
