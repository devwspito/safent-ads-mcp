"""`ScoreCreativeFatigue`: M16 (CTR vs 14D) y M21 (hook/hold) sobre una
creatividad."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from safent_ads.signals.application.score_creative_fatigue import (
    ScoreCreativeFatigue,
    ScoreCreativeFatigueRequest,
)
from safent_ads.signals.domain.gate_verdict import GateName, GateVerdict
from safent_ads.signals.domain.metric_window import MetricWindow
from safent_ads.signals.domain.signal import CreativeSignalKind
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.testing.in_memory_metric_window_repository import (
    InMemoryMetricWindowRepository,
)
from safent_ads.signals.testing.in_memory_signal_repository import (
    InMemoryCreativeSignalRepository,
)

_ENTITY = EntityRef(platform=PlatformCode.META, level=EntityLevel.CREATIVE, external_id="cr-1")
_AS_OF = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
_PASSING_GATES = (GateVerdict.ok(GateName.LEARNING),)


def _window(span: WindowSpan, **overrides: object) -> MetricWindow:
    defaults: dict[str, object] = {
        "entity_ref": _ENTITY,
        "span": span,
        "spend_minor": 5_000,
        "impressions": 10_000,
        "clicks": 100,
        "reach": 4_000,
        "conversions": 2,
        "conversion_value_minor": 4_000,
    }
    defaults.update(overrides)
    return MetricWindow(**defaults)  # type: ignore[arg-type]


async def test_detects_fatigue_when_ctr_drops_versus_baseline() -> None:
    windows = InMemoryMetricWindowRepository(
        {
            (_ENTITY, WindowSpan.D7): _window(WindowSpan.D7, impressions=10_000, clicks=100),
            (_ENTITY, WindowSpan.D14): _window(WindowSpan.D14, impressions=20_000, clicks=300),
        }
    )
    signals = InMemoryCreativeSignalRepository()
    use_case = ScoreCreativeFatigue(windows, signals)
    request = ScoreCreativeFatigueRequest(
        entity_ref=_ENTITY, currency="EUR", gate_verdicts=_PASSING_GATES, as_of=_AS_OF
    )

    signal = await use_case.execute(request)

    assert signal is not None
    assert signal.kind == CreativeSignalKind.FATIGUE
    assert signals.saved == [signal]


async def test_returns_none_and_does_not_save_when_no_fatigue_or_winner() -> None:
    stable_7d = _window(
        WindowSpan.D7, impressions=10_000, clicks=150, video_views_3s=2_500
    )  # ctr 1.5%, hook rate 25% (ni kill ni scale)
    stable_14d = _window(WindowSpan.D14, impressions=20_000, clicks=300)  # ctr 1.5%
    windows = InMemoryMetricWindowRepository(
        {(_ENTITY, WindowSpan.D7): stable_7d, (_ENTITY, WindowSpan.D14): stable_14d}
    )
    signals = InMemoryCreativeSignalRepository()
    use_case = ScoreCreativeFatigue(windows, signals)
    request = ScoreCreativeFatigueRequest(
        entity_ref=_ENTITY, currency="EUR", gate_verdicts=_PASSING_GATES, as_of=_AS_OF
    )

    signal = await use_case.execute(request)

    assert signal is None
    assert signals.saved == []
