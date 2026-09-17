"""`rule_step._resolve_firing`: la `Cause` de una propuesta nacida de una
regla debe enlazar con la `Signal` que la disparo (`Cause.signal_id`).

Regresion: `_resolve_firing` construia `Cause(signal_id=None)` a mano,
descartando el `signal.signal_id` que ya traia la senal leida de vuelta
--`SqlSignalResolutionPort`/`recalibrate_rules` se quedaban sin enlace para
contrastar la senal contra su desenlace (T199)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.budget import Budget, BudgetKind
from safent_ads.accounts.domain.money import Money as AccountsMoney
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.orchestration.infrastructure.rule_step import _resolve_firing
from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.testing.in_memory_repositories import InMemoryRuleRepository
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from safent_ads.signals.domain.cause import Cause as SignalCause
from safent_ads.signals.domain.signal import Signal, SignalKind
from safent_ads.signals.domain.value_objects import Evidence, MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.testing.in_memory_signal_repository import InMemorySignalRepository

_NOW = datetime(2026, 9, 9, tzinfo=UTC)
_ENTITY_REF = EntityRef(PlatformCode.META, EntityLevel.CAMPAIGN, "c-signal-link")
_RULE_CODE = "M05"
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


def _entity() -> AdEntity:
    return AdEntity(
        business_id=BusinessId.new(),
        entity_ref=_ENTITY_REF,
        parent_ref=EntityRef(PlatformCode.META, EntityLevel.ACCOUNT, "act_1"),
        name="Campana M05",
        status=AdEntityStatus.ACTIVE,
        platform_state_hash=PlatformStateHash.compute({"daily_budget_minor": 10_000}),
        is_controllable=True,
        budget=Budget(amount=AccountsMoney(10_000, "EUR"), kind=BudgetKind.DAILY),
    )


def _signal(*, signal_id: str | None) -> Signal:
    return Signal(
        entity_ref=_ENTITY_REF,
        kind=SignalKind.SELL,
        strength=SignalStrength(70),
        cause=SignalCause.ROAS_BELOW_TARGET_SUSTAINED,
        cause_sentence="ROAS por debajo del objetivo en 7D",
        span=WindowSpan.D7,
        money_at_stake=MoneyAtStake(minor_units=12_000, currency="EUR"),
        evidence=Evidence(
            metric="roas", actual=1.4, target=2.0, baseline=None, span=WindowSpan.D7
        ),
        gate_verdicts=(),
        emitted_at=_NOW,
        rule_code=_RULE_CODE,
        signal_id=signal_id,
    )


def _rule() -> Rule:
    return Rule(
        code=_RULE_CODE,
        platform=PlatformCode.META,
        entity_level=EntityLevel.CAMPAIGN,
        description="ROAS por debajo del objetivo",
        condition=_CONDITION,
        action_kind=ActionKind.SELL,
        magnitude_pct=30,
        autonomy_level=AutonomyLevel.AUTO,
        cooldown=timedelta(hours=24),
        source_url="https://bir.ch/facebook-automated-rules",
    )


async def _enabled_rule_repository() -> InMemoryRuleRepository:
    repo = InMemoryRuleRepository()
    rule = _rule()
    await repo.sync_catalog([rule])
    await repo.set_autonomy(code=rule.code, level=rule.autonomy_level, enabled=True)
    return repo


async def test_resolve_firing_links_cause_to_the_signal_that_fired_it() -> None:
    rules = await _enabled_rule_repository()
    signals = InMemorySignalRepository()
    await signals.save(_signal(signal_id="11111111-1111-1111-1111-111111111111"))

    firing = await _resolve_firing(_entity(), rules, signals)

    assert firing is not None
    _, _, _, cause = firing
    assert cause.signal_id == "11111111-1111-1111-1111-111111111111"
    assert cause.rule_id == _RULE_CODE


async def test_resolve_firing_keeps_cause_signal_id_none_when_signal_lacks_it() -> None:
    """Una senal recien emitida, todavia sin `id` de Postgres, no debe
    fabricar un enlace falso."""
    rules = await _enabled_rule_repository()
    signals = InMemorySignalRepository()
    await signals.save(_signal(signal_id=None))

    firing = await _resolve_firing(_entity(), rules, signals)

    assert firing is not None
    _, _, _, cause = firing
    assert cause.signal_id is None
