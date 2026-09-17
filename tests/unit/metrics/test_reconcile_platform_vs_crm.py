"""`ReconcilePlatformVsCrm` (tasks.md T116) contra los dobles en memoria de
`metrics.testing` -- mismo patron que
`tests/unit/optimization/application/test_evaluate_signal_outcomes.py`
para T199."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import MappingProxyType

from safent_ads.metrics.application.ports import ActionableSignalRef
from safent_ads.metrics.application.reconcile_platform_vs_crm import ReconcilePlatformVsCrm
from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.testing.in_memory_metric_fact_repository import (
    InMemoryMetricFactRepository,
)
from safent_ads.metrics.testing.in_memory_reconciliation_repositories import (
    InMemoryActionableSignalPort,
    InMemoryCrmConversionsPort,
    InMemorySignalContradictionRecorder,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = BusinessId.new()
_ENTITY = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")
_WINDOW_START = date(2026, 8, 1)
_WINDOW_END = date(2026, 8, 7)
_NOW = datetime(2026, 8, 10, tzinfo=UTC)


def _fact(*, conversions_business_conversion: int) -> MetricFact:
    return MetricFact(
        entity_ref=_ENTITY,
        stat_date=_WINDOW_START,
        account_timezone="Europe/Madrid",
        currency="EUR",
        spend_minor=8_000,
        impressions=1_000,
        clicks=50,
        reach=900,
        conversions=MappingProxyType(
            {ConversionKind.BUSINESS_CONVERSION: conversions_business_conversion}
        ),
        ingested_at=_NOW,
    )


def _ref(signal_id: str = "sig-1", rule_code: str = "M05") -> ActionableSignalRef:
    return ActionableSignalRef(
        signal_id=signal_id,
        entity_ref=_ENTITY,
        account_id="acc-1",
        rule_code=rule_code,
        window_start=_WINDOW_START,
        window_end=_WINDOW_END,
    )


class _Harness:
    def __init__(self) -> None:
        self.metrics = InMemoryMetricFactRepository()
        self.actionable_signals = InMemoryActionableSignalPort()
        self.crm_conversions = InMemoryCrmConversionsPort()
        self.recorder = InMemorySignalContradictionRecorder()
        self.use_case = ReconcilePlatformVsCrm(
            metrics=self.metrics,
            crm_conversions=self.crm_conversions,
            actionable_signals=self.actionable_signals,
            recorder=self.recorder,
            clock=FixedClock(_NOW),
        )


async def test_contradicts_a_signal_when_crm_disproves_the_platform_claim() -> None:
    h = _Harness()
    await h.metrics.upsert_many([_fact(conversions_business_conversion=10)])
    h.actionable_signals.seed(business_id=_BUSINESS_ID, refs=[_ref()])
    h.crm_conversions.seed(entity_ref=_ENTITY, count=1)

    contradicted = await h.use_case.execute(business_id=_BUSINESS_ID, cutoff=_NOW)

    assert len(contradicted) == 1
    event = contradicted[0]
    assert event.signal_id == "sig-1"
    assert event.rule_code == "M05"
    assert event.reconciliation.platform_conversions == 10
    assert event.reconciliation.crm_conversions == 1
    assert h.recorder.recorded == [event]


async def test_does_not_contradict_when_crm_confirms_the_platform() -> None:
    h = _Harness()
    await h.metrics.upsert_many([_fact(conversions_business_conversion=10)])
    h.actionable_signals.seed(business_id=_BUSINESS_ID, refs=[_ref()])
    h.crm_conversions.seed(entity_ref=_ENTITY, count=9)

    contradicted = await h.use_case.execute(business_id=_BUSINESS_ID, cutoff=_NOW)

    assert contradicted == ()
    assert h.recorder.recorded == []


async def test_no_candidate_signals_records_nothing() -> None:
    h = _Harness()

    contradicted = await h.use_case.execute(business_id=_BUSINESS_ID, cutoff=_NOW)

    assert contradicted == ()
    assert h.recorder.recorded == []


async def test_reconciles_each_candidate_signal_independently() -> None:
    h = _Harness()
    await h.metrics.upsert_many([_fact(conversions_business_conversion=10)])
    h.actionable_signals.seed(
        business_id=_BUSINESS_ID, refs=[_ref(signal_id="sig-1"), _ref(signal_id="sig-2")]
    )
    h.crm_conversions.seed(entity_ref=_ENTITY, count=1)

    contradicted = await h.use_case.execute(business_id=_BUSINESS_ID, cutoff=_NOW)

    assert {event.signal_id for event in contradicted} == {"sig-1", "sig-2"}
