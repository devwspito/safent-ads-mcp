"""`EvaluateEntitySignals`: puertas -> catalogo -> persistencia
(`test_signal_carries_window_cause_stake`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from safent_ads.signals.application.evaluate_entity_signals import (
    EvaluateEntitySignals,
    EvaluateEntitySignalsRequest,
    GateContext,
    SignalTargets,
)
from safent_ads.signals.domain.gates import LearningStatus
from safent_ads.signals.domain.metric_window import MetricWindow
from safent_ads.signals.domain.signal import SignalKind
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.testing.in_memory_metric_window_repository import (
    InMemoryMetricWindowRepository,
)
from safent_ads.signals.testing.in_memory_signal_repository import InMemorySignalRepository

_ENTITY = EntityRef(platform=PlatformCode.META, level=EntityLevel.AD_SET, external_id="as-1")
_AS_OF = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)


def _window(span: WindowSpan, **overrides: object) -> MetricWindow:
    defaults: dict[str, object] = {
        "entity_ref": _ENTITY,
        "span": span,
        "spend_minor": 10_000,
        "impressions": 5_000,
        "clicks": 100,
        "reach": 2_000,
        "conversions": 5,
        "conversion_value_minor": 10_000,
    }
    defaults.update(overrides)
    return MetricWindow(**defaults)  # type: ignore[arg-type]


def _gate_context(**overrides: object) -> GateContext:
    defaults: dict[str, object] = {
        "learning_status": LearningStatus.SUCCESS,
        "last_change_at": None,
        "cooldown": timedelta(hours=24),
        "median_lag_days": 3,
        "min_spend_multiple": 5.0,
        "min_impressions": 1_000,
        "min_conversions": 10,
    }
    defaults.update(overrides)
    return GateContext(**defaults)  # type: ignore[arg-type]


async def test_signal_carries_window_cause_stake() -> None:
    windows = InMemoryMetricWindowRepository(
        {
            (_ENTITY, WindowSpan.D3): _window(
                WindowSpan.D3, spend_minor=3_000, conversion_value_minor=3_000
            ),
            (_ENTITY, WindowSpan.D7): _window(
                WindowSpan.D7, spend_minor=10_000, conversion_value_minor=10_000
            ),
            (_ENTITY, WindowSpan.D30): _window(WindowSpan.D30),
        }
    )
    signals = InMemorySignalRepository()
    use_case = EvaluateEntitySignals(windows, signals)
    request = EvaluateEntitySignalsRequest(
        entity_ref=_ENTITY,
        targets=SignalTargets(currency="EUR", target_cpa_minor=1_000, target_roas=2.0),
        gate_context=_gate_context(),
        as_of=_AS_OF,
    )

    signal = await use_case.execute(request)

    assert signal.kind == SignalKind.SELL  # M05: ROAS=1.0 < target=2.0 en 3D y 7D
    assert signal.span == WindowSpan.D7
    assert signal.cause is not None
    assert signal.cause_sentence != ""
    assert signal.money_at_stake.minor_units == 10_000
    assert signals.saved == [signal]


async def test_hold_when_learning_gate_blocks() -> None:
    windows = InMemoryMetricWindowRepository({(_ENTITY, WindowSpan.D7): _window(WindowSpan.D7)})
    signals = InMemorySignalRepository()
    use_case = EvaluateEntitySignals(windows, signals)
    request = EvaluateEntitySignalsRequest(
        entity_ref=_ENTITY,
        targets=SignalTargets(currency="EUR", target_cpa_minor=1_000, target_roas=2.0),
        gate_context=_gate_context(learning_status=LearningStatus.LEARNING),
        as_of=_AS_OF,
    )

    signal = await use_case.execute(request)

    assert signal.kind == SignalKind.HOLD
    assert any(not verdict.passed for verdict in signal.gate_verdicts)


async def test_hold_when_no_catalog_condition_matches() -> None:
    on_target_window = _window(
        WindowSpan.D7, spend_minor=6_000, conversion_value_minor=18_000, conversions=10
    )  # roas=3.0 >= target, cumple MinDataGate por conversiones
    windows = InMemoryMetricWindowRepository(
        {
            (_ENTITY, WindowSpan.D3): on_target_window,
            (_ENTITY, WindowSpan.D7): on_target_window,
            (_ENTITY, WindowSpan.D30): on_target_window,
        }
    )
    signals = InMemorySignalRepository()
    use_case = EvaluateEntitySignals(windows, signals)
    request = EvaluateEntitySignalsRequest(
        entity_ref=_ENTITY,
        targets=SignalTargets(currency="EUR", target_cpa_minor=1_000, target_roas=2.0),
        gate_context=_gate_context(),
        as_of=_AS_OF,
    )

    signal = await use_case.execute(request)

    assert signal.kind == SignalKind.HOLD
    assert all(verdict.passed for verdict in signal.gate_verdicts)
