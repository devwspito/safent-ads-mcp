"""`signal_engine`: casos representativos del catalogo M05, M07, M09, M13,
M16, M21, G01, G04, G06, X01 (tasks.md T035, rule-catalog-and-signals.md §2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from safent_ads.signals.domain.cause import Cause
from safent_ads.signals.domain.gate_verdict import GateName, GateVerdict
from safent_ads.signals.domain.metric_window import MetricWindow
from safent_ads.signals.domain.signal import CreativeSignalKind, SignalKind
from safent_ads.signals.domain.signal_engine import (
    RuleContext,
    evaluate_g01,
    evaluate_g04,
    evaluate_g06,
    evaluate_m05,
    evaluate_m07,
    evaluate_m09,
    evaluate_m13,
    evaluate_m16,
    evaluate_m21,
    evaluate_x01,
    hold_signal,
)
from safent_ads.signals.domain.window_span import WindowSpan

_ENTITY = EntityRef(platform=PlatformCode.META, level=EntityLevel.AD_SET, external_id="e1")
_AS_OF = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
_PASSING_GATES = (GateVerdict.ok(GateName.LEARNING), GateVerdict.ok(GateName.MIN_DATA))


def _context(gate_verdicts: tuple[GateVerdict, ...] = _PASSING_GATES) -> RuleContext:
    return RuleContext(
        entity_ref=_ENTITY, currency="EUR", gate_verdicts=gate_verdicts, as_of=_AS_OF
    )


def _window(span: WindowSpan, **overrides: object) -> MetricWindow:
    defaults: dict[str, object] = {
        "entity_ref": _ENTITY,
        "span": span,
        "spend_minor": 10_000,
        "impressions": 5_000,
        "clicks": 100,
        "reach": 2_000,
        "conversions": 5,
        "conversion_value_minor": 20_000,
    }
    defaults.update(overrides)
    return MetricWindow(**defaults)  # type: ignore[arg-type]


def test_hold_signal_on_gate_failure() -> None:
    failing_gates = (GateVerdict.blocked(GateName.LEARNING, "en aprendizaje"),)

    signal = hold_signal(_context(failing_gates), span=WindowSpan.D7)

    assert signal.kind == SignalKind.HOLD
    assert signal.cause == Cause.GATE_BLOCKED
    assert "aprendizaje" in signal.cause_sentence
    assert signal.strength.value == 0


def test_hold_signal_inside_target_band() -> None:
    signal = hold_signal(_context(), span=WindowSpan.D7)

    assert signal.kind == SignalKind.HOLD
    assert signal.cause == Cause.INSIDE_TARGET_BAND


def test_m05_sell_when_roas_below_target_on_3d_and_7d() -> None:
    window_3d = _window(WindowSpan.D3, spend_minor=3_000, conversion_value_minor=3_000)
    window_7d = _window(WindowSpan.D7, spend_minor=10_000, conversion_value_minor=10_000)

    signal = evaluate_m05(_context(), window_3d=window_3d, window_7d=window_7d, target_roas=2.0)

    assert signal is not None
    assert signal.kind == SignalKind.SELL
    assert signal.cause == Cause.ROAS_BELOW_TARGET_SUSTAINED
    assert signal.rule_code == "M05"
    assert signal.money_at_stake.minor_units == 10_000


def test_m05_none_when_only_one_window_below_target() -> None:
    window_3d = _window(WindowSpan.D3, spend_minor=1_000, conversion_value_minor=3_000)  # roas 3.0
    window_7d = _window(
        WindowSpan.D7, spend_minor=10_000, conversion_value_minor=10_000
    )  # roas 1.0

    assert (
        evaluate_m05(_context(), window_3d=window_3d, window_7d=window_7d, target_roas=2.0) is None
    )


def test_m07_exit_when_cpa_between_1_5_and_2x_target_post_learning() -> None:
    window_7d = _window(WindowSpan.D7, spend_minor=17_000, conversions=10)  # cpa=1700

    signal = evaluate_m07(_context(), window_7d=window_7d, target_cpa_minor=1_000)

    assert signal is not None
    assert signal.kind == SignalKind.EXIT
    assert signal.rule_code == "M07"


def test_m07_none_when_cpa_above_2x_target() -> None:
    window_7d = _window(WindowSpan.D7, spend_minor=25_000, conversions=10)  # cpa=2500, ratio=2.5

    assert evaluate_m07(_context(), window_7d=window_7d, target_cpa_minor=1_000) is None


def test_m09_exit_when_zero_conversions_and_spend_over_multiple() -> None:
    window_7d = _window(WindowSpan.D7, spend_minor=6_000, conversions=0)

    signal = evaluate_m09(_context(), window_7d=window_7d, target_cpa_minor=1_000)

    assert signal is not None
    assert signal.kind == SignalKind.EXIT
    assert signal.cause == Cause.ZERO_CONVERSIONS_SPEND_MULTIPLE


def test_m09_none_when_below_spend_multiple() -> None:
    window_7d = _window(WindowSpan.D7, spend_minor=2_000, conversions=0)

    assert evaluate_m09(_context(), window_7d=window_7d, target_cpa_minor=1_000) is None


def test_m13_exit_on_high_frequency_low_ctr_and_impressions() -> None:
    window_7d = _window(
        WindowSpan.D7,
        impressions=10_000,
        reach=2_000,
        clicks=50,
        conversions=0,
        conversion_value_minor=0,
    )  # frequency=5.0, ctr=0.005

    signal = evaluate_m13(_context(), window_7d=window_7d)

    assert signal is not None
    assert signal.kind == SignalKind.EXIT
    assert signal.rule_code == "M13"


def test_m13_none_when_frequency_within_bounds() -> None:
    window_7d = _window(WindowSpan.D7, impressions=10_000, reach=5_000, clicks=50)  # freq=2.0

    assert evaluate_m13(_context(), window_7d=window_7d) is None


def test_m16_fatigue_when_ctr_drops_versus_14d_baseline() -> None:
    window_7d = _window(WindowSpan.D7, impressions=10_000, clicks=100)  # ctr=1%
    window_14d = _window(WindowSpan.D14, impressions=20_000, clicks=300)  # ctr=1.5%

    signal = evaluate_m16(_context(), window_7d=window_7d, window_14d=window_14d)

    assert signal is not None
    assert signal.kind == CreativeSignalKind.FATIGUE
    assert signal.rule_code == "M16"


def test_m16_none_when_ctr_stable() -> None:
    window_7d = _window(WindowSpan.D7, impressions=10_000, clicks=150)  # ctr=1.5%
    window_14d = _window(WindowSpan.D14, impressions=20_000, clicks=300)  # ctr=1.5%

    assert evaluate_m16(_context(), window_7d=window_7d, window_14d=window_14d) is None


def test_m21_kill_when_hook_rate_below_20_percent() -> None:
    window_7d = _window(
        WindowSpan.D7, impressions=5_000, video_views_3s=500, video_views_75pct=100
    )  # hook=10%

    signal = evaluate_m21(_context(), window_7d=window_7d)

    assert signal is not None
    assert signal.kind == CreativeSignalKind.LOSER
    assert signal.cause == Cause.HOOK_RATE_LOW


def test_m21_scale_when_hook_and_hold_rate_high() -> None:
    window_7d = _window(
        WindowSpan.D7, impressions=5_000, video_views_3s=2_000, video_views_75pct=1_000
    )  # hook=40%, hold=50%

    signal = evaluate_m21(_context(), window_7d=window_7d)

    assert signal is not None
    assert signal.kind == CreativeSignalKind.WINNER
    assert signal.cause == Cause.HOOK_AND_HOLD_RATE_HIGH


def test_m21_none_below_minimum_impressions() -> None:
    window_7d = _window(
        WindowSpan.D7, impressions=500, video_views_3s=50, video_views_75pct=10
    )  # hook=10% pero pocas impresiones

    assert evaluate_m21(_context(), window_7d=window_7d) is None


def test_g01_buy_when_limited_by_budget_and_at_target() -> None:
    window_7d = _window(
        WindowSpan.D7,
        spend_minor=10_000,
        conversion_value_minor=20_000,
        search_lost_is_budget_pct=35.0,
    )  # roas=2.0

    signal = evaluate_g01(_context(), window_7d=window_7d, target_roas=2.0)

    assert signal is not None
    assert signal.kind == SignalKind.BUY
    assert signal.rule_code == "G01"


def test_g01_none_when_not_limited_by_budget() -> None:
    window_7d = _window(
        WindowSpan.D7,
        spend_minor=10_000,
        conversion_value_minor=20_000,
        search_lost_is_budget_pct=5.0,
    )

    assert evaluate_g01(_context(), window_7d=window_7d, target_roas=2.0) is None


def test_g04_sell_when_cpa_30d_above_120_percent_target() -> None:
    window_30d = _window(WindowSpan.D30, spend_minor=130_000, conversions=100)  # cpa=1300

    signal = evaluate_g04(_context(), window_30d=window_30d, target_cpa_minor=1_000)

    assert signal is not None
    assert signal.kind == SignalKind.SELL
    assert signal.rule_code == "G04"


def test_g04_none_when_cpa_within_120_percent() -> None:
    window_30d = _window(WindowSpan.D30, spend_minor=110_000, conversions=100)  # cpa=1100

    assert evaluate_g04(_context(), window_30d=window_30d, target_cpa_minor=1_000) is None


def test_g06_exit_when_search_term_non_converting_over_cost_threshold() -> None:
    window_30d = _window(
        WindowSpan.D30, clicks=20, conversions=0, spend_minor=3_000, conversion_value_minor=0
    )

    signal = evaluate_g06(_context(), window_30d=window_30d, target_cpa_minor=1_000, min_clicks=10)

    assert signal is not None
    assert signal.kind == SignalKind.EXIT
    assert signal.cause == Cause.SEARCH_TERM_NON_CONVERTING


def test_g06_none_when_clicks_below_minimum() -> None:
    window_30d = _window(WindowSpan.D30, clicks=5, conversions=0, spend_minor=3_000)

    assert (
        evaluate_g06(_context(), window_30d=window_30d, target_cpa_minor=1_000, min_clicks=10)
        is None
    )


def test_x01_hold_when_daily_spend_spikes() -> None:
    signal = evaluate_x01(_context(), today_spend_minor=10_000, avg_daily_spend_minor=2_000.0)

    assert signal is not None
    assert signal.kind == SignalKind.HOLD
    assert signal.cause == Cause.DAILY_SPEND_SPIKE


def test_x01_none_when_spend_within_normal_range() -> None:
    assert evaluate_x01(_context(), today_spend_minor=4_000, avg_daily_spend_minor=2_000.0) is None


@pytest.mark.parametrize(
    "evaluator_call",
    [
        lambda: evaluate_m05(
            _context(),
            window_3d=_window(WindowSpan.D3),
            window_7d=_window(WindowSpan.D7),
            target_roas=2.0,
        ),
        lambda: evaluate_m07(_context(), window_7d=_window(WindowSpan.D7), target_cpa_minor=1_000),
    ],
)
def test_every_emitted_signal_carries_gate_verdicts(evaluator_call: object) -> None:
    result = evaluator_call()  # type: ignore[operator]
    if result is not None:
        assert result.gate_verdicts == _PASSING_GATES
