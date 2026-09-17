"""`EvaluateSignalOutcomes` (profitability-engine.md §6, tasks.md T199)
contra los dobles en memoria de `optimization/testing` -- idempotencia,
grupo de control gratis y la guarda de auto-confirmacion."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.optimization.application.evaluate_signal_outcomes import EvaluateSignalOutcomes
from safent_ads.optimization.application.ports import DueSignalOutcome, EntityCpaWindowSnapshot
from safent_ads.optimization.domain.calibration import OutcomeSource
from safent_ads.optimization.testing.in_memory_repositories import (
    InMemoryEntityCpaWindowPort,
    InMemoryPendingSignalOutcomesPort,
    InMemoryRuleActionKindPort,
    InMemorySignalOutcomeRepository,
    InMemorySignalResolutionPort,
)
from safent_ads.rules.domain.autonomy import ActionKind
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = BusinessId.new()
_ENTITY_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")
_EMITTED_AT = datetime(2026, 8, 20, tzinfo=UTC)
_NOW = datetime(2026, 9, 9, tzinfo=UTC)  # > 14 dias despues de _EMITTED_AT


def _candidate(signal_id: str = "sig-1", rule_code: str = "G01") -> DueSignalOutcome:
    return DueSignalOutcome(
        signal_id=signal_id,
        business_id=_BUSINESS_ID,
        account_id="acc-1",
        entity_ref=_ENTITY_REF,
        rule_code=rule_code,
        emitted_at=_EMITTED_AT,
    )


class _Harness:
    def __init__(self) -> None:
        self.due = InMemoryPendingSignalOutcomesPort()
        self.resolution = InMemorySignalResolutionPort()
        self.cpa_window = InMemoryEntityCpaWindowPort()
        self.action_kinds = InMemoryRuleActionKindPort()
        self.outcomes = InMemorySignalOutcomeRepository()
        self.use_case = EvaluateSignalOutcomes(
            due_signals=self.due,
            resolution=self.resolution,
            cpa_window=self.cpa_window,
            action_kinds=self.action_kinds,
            outcomes=self.outcomes,
            clock=FixedClock(_NOW),
        )


async def test_control_group_confirms_a_defensive_rule_when_entity_stayed_bad() -> None:
    h = _Harness()
    h.due.seed(business_id=_BUSINESS_ID, due=[_candidate()])
    h.resolution.seed(signal_id="sig-1", source=OutcomeSource.EXPIRED)
    h.action_kinds.seed(rule_code="G01", action_kind=ActionKind.SELL)
    h.cpa_window.seed(
        entity_ref=_ENTITY_REF,
        snapshot=EntityCpaWindowSnapshot(spend=100.0, leads=1, target_cpa=10.0, window_days=14),
    )

    evaluated = await h.use_case.execute(business_id=_BUSINESS_ID)

    assert evaluated == 1
    assert len(h.outcomes.recorded) == 1
    outcome = h.outcomes.recorded[0]
    assert outcome.outcome_source is OutcomeSource.EXPIRED
    assert outcome.was_correct is True


async def test_applied_defensive_action_is_inconclusive_not_auto_confirmed() -> None:
    h = _Harness()
    h.due.seed(business_id=_BUSINESS_ID, due=[_candidate()])
    h.resolution.seed(signal_id="sig-1", source=OutcomeSource.APPLIED)
    h.action_kinds.seed(rule_code="G01", action_kind=ActionKind.SELL)
    h.cpa_window.seed(
        entity_ref=_ENTITY_REF,
        snapshot=EntityCpaWindowSnapshot(spend=5.0, leads=2, target_cpa=10.0, window_days=14),
    )

    await h.use_case.execute(business_id=_BUSINESS_ID)

    assert h.outcomes.recorded[0].was_correct is None


async def test_unknown_rule_code_is_inconclusive() -> None:
    h = _Harness()
    h.due.seed(business_id=_BUSINESS_ID, due=[_candidate(rule_code="UNKNOWN")])
    h.cpa_window.seed(
        entity_ref=_ENTITY_REF,
        snapshot=EntityCpaWindowSnapshot(spend=100.0, leads=1, target_cpa=10.0, window_days=14),
    )

    await h.use_case.execute(business_id=_BUSINESS_ID)

    assert h.outcomes.recorded[0].was_correct is None


async def test_no_cpa_data_is_inconclusive() -> None:
    h = _Harness()
    h.due.seed(business_id=_BUSINESS_ID, due=[_candidate()])
    h.action_kinds.seed(rule_code="G01", action_kind=ActionKind.SELL)

    await h.use_case.execute(business_id=_BUSINESS_ID)

    assert h.outcomes.recorded[0].was_correct is None


async def test_only_signals_past_the_horizon_are_evaluated() -> None:
    h = _Harness()
    recent = _candidate(signal_id="sig-recent")
    recent = DueSignalOutcome(
        signal_id=recent.signal_id,
        business_id=recent.business_id,
        account_id=recent.account_id,
        entity_ref=recent.entity_ref,
        rule_code=recent.rule_code,
        emitted_at=_NOW,  # emitida hoy: dentro de la ventana de 14 dias
    )
    h.due.seed(business_id=_BUSINESS_ID, due=[_candidate(), recent])

    evaluated = await h.use_case.execute(business_id=_BUSINESS_ID)

    assert evaluated == 1
    assert h.outcomes.recorded[0].signal_id == "sig-1"
