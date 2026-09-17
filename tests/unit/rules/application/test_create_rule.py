"""`CreateRule` (`POST /rules`): construir `Rule` revalida el invariante
FR-11/FR-12 (AUTO nunca sube gasto) antes de persistir nada -- misma regla
que ya protege `PUT /rules/{id}` (`apply_autonomy_level`), aqui a la
entrada en vez de a la actualizacion."""

from __future__ import annotations

from datetime import timedelta

import pytest

from safent_ads.rules.application.create_rule import CreateRule, CreateRuleCommand
from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind
from safent_ads.rules.domain.errors import SpendIncreasingAutoRuleError
from safent_ads.rules.infrastructure.errors import DuplicateRuleCodeError
from safent_ads.rules.testing.in_memory_repositories import InMemoryRuleRepository
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


def _command(**overrides: object) -> CreateRuleCommand:
    defaults: dict[str, object] = {
        "code": "X95",
        "platform": PlatformCode.META,
        "entity_level": EntityLevel.AD_SET,
        "description": "Regla de prueba",
        "condition": _CONDITION,
        "action_kind": ActionKind.SELL,
        "magnitude_pct": 20.0,
        "autonomy_level": AutonomyLevel.NOTIFY,
        "cooldown": timedelta(hours=6),
        "source_url": "https://example.test/rule",
        "enabled": False,
    }
    defaults.update(overrides)
    return CreateRuleCommand(**defaults)  # type: ignore[arg-type]


async def test_persists_a_new_rule_and_returns_it() -> None:
    rules = InMemoryRuleRepository()

    stored = await CreateRule(rules).execute(_command())

    assert stored.code == "X95"
    assert stored.is_enabled is False
    assert await rules.get_by_code("X95") == stored


async def test_rejects_auto_autonomy_on_a_spend_increasing_action_before_persisting() -> None:
    rules = InMemoryRuleRepository()
    command = _command(action_kind=ActionKind.BUY, autonomy_level=AutonomyLevel.AUTO)

    with pytest.raises(SpendIncreasingAutoRuleError):
        await CreateRule(rules).execute(command)

    assert await rules.get_by_code("X95") is None


async def test_rejects_a_duplicate_code() -> None:
    rules = InMemoryRuleRepository()
    await CreateRule(rules).execute(_command())

    with pytest.raises(DuplicateRuleCodeError):
        await CreateRule(rules).execute(_command())


async def test_a_defensive_action_may_be_created_as_auto() -> None:
    rules = InMemoryRuleRepository()
    command = _command(action_kind=ActionKind.SELL, autonomy_level=AutonomyLevel.AUTO, enabled=True)

    stored = await CreateRule(rules).execute(command)

    assert stored.rule.autonomy_level is AutonomyLevel.AUTO
    assert stored.is_enabled is True
