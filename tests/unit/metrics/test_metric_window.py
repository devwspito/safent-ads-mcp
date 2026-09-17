"""`MetricWindow`: agregacion como razon de sumas, nunca media de razones
(rule-catalog-and-signals.md §3)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.date_window import DateWindow
from safent_ads.metrics.domain.errors import EmptyMetricWindowError
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.domain.metric_window import MetricWindow
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_ENTITY = EntityRef(platform=PlatformCode.META, level=EntityLevel.AD, external_id="a1")
_OTHER_ENTITY = EntityRef(platform=PlatformCode.META, level=EntityLevel.AD, external_id="a2")
_NOW = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)


def _fact(day: int, **overrides: object) -> MetricFact:
    defaults: dict[str, object] = {
        "entity_ref": _ENTITY,
        "stat_date": date(2026, 9, day),
        "account_timezone": "Europe/Madrid",
        "currency": "EUR",
        "spend_minor": 1_000,
        "impressions": 100,
        "clicks": 10,
        "reach": 80,
        "ingested_at": _NOW,
    }
    defaults.update(overrides)
    return MetricFact(**defaults)  # type: ignore[arg-type]


def test_from_facts_sums_only_matching_entity_within_window() -> None:
    window = DateWindow(start_date=date(2026, 9, 1), end_date=date(2026, 9, 3))
    facts = [
        _fact(1, spend_minor=1_000, impressions=100, clicks=10),
        _fact(2, spend_minor=2_000, impressions=200, clicks=20),
        _fact(4, spend_minor=9_999, impressions=999, clicks=99),  # fuera de ventana
        _fact(1, entity_ref=_OTHER_ENTITY, spend_minor=5_000),  # otra entidad
    ]

    result = MetricWindow.from_facts(entity_ref=_ENTITY, window=window, facts=facts)

    assert result.spend_minor == 3_000
    assert result.impressions == 300
    assert result.clicks == 30
    assert result.fact_count == 2


def test_from_facts_raises_when_no_fact_matches() -> None:
    window = DateWindow(start_date=date(2026, 9, 1), end_date=date(2026, 9, 3))

    with pytest.raises(EmptyMetricWindowError):
        MetricWindow.from_facts(entity_ref=_ENTITY, window=window, facts=[])


def test_derived_rates_are_ratio_of_sums_not_mean_of_ratios() -> None:
    window = DateWindow(start_date=date(2026, 9, 1), end_date=date(2026, 9, 2))
    # Dia 1: CTR altisimo con pocas impresiones; dia 2: CTR bajo con muchas.
    # Media de razones daria ~27.5%; razon de sumas da 200/11_000 = 1.818...%.
    facts = [
        _fact(1, spend_minor=500, impressions=100, clicks=50, reach=50),
        _fact(2, spend_minor=10_000, impressions=10_900, clicks=150, reach=9_000),
    ]

    result = MetricWindow.from_facts(entity_ref=_ENTITY, window=window, facts=facts)

    assert result.impressions == 11_000
    assert result.clicks == 200
    assert result.ctr == pytest.approx(200 / 11_000)
    assert result.cpc_minor == pytest.approx(10_500 / 200)
    assert result.cpm_minor == pytest.approx(10_500 / 11_000 * 1000)


def test_conversion_rates_and_video_rates() -> None:
    window = DateWindow(start_date=date(2026, 9, 1), end_date=date(2026, 9, 1))
    fact = _fact(
        1,
        spend_minor=10_000,
        conversion_value_minor=40_000,
        video_views_3s=400,
        video_views_75pct=100,
        conversions={ConversionKind.LEAD: 5, ConversionKind.BUSINESS_CONVERSION: 1},
    )

    result = MetricWindow.from_facts(entity_ref=_ENTITY, window=window, facts=[fact])

    assert result.roas == pytest.approx(4.0)
    assert result.cpl_minor() == pytest.approx(2_000.0)
    assert result.cpa_minor(ConversionKind.BUSINESS_CONVERSION) == pytest.approx(10_000.0)
    assert result.cpa_minor(ConversionKind.CALL) is None
    assert result.hook_rate == pytest.approx(400 / 100)
    assert result.hold_rate == pytest.approx(100 / 400)


def test_rates_are_none_on_zero_denominator() -> None:
    window = DateWindow(start_date=date(2026, 9, 1), end_date=date(2026, 9, 1))
    fact = _fact(
        1, spend_minor=0, impressions=0, clicks=0, reach=0, video_views_3s=0, video_views_75pct=0
    )

    result = MetricWindow.from_facts(entity_ref=_ENTITY, window=window, facts=[fact])

    assert result.ctr is None
    assert result.cpc_minor is None
    assert result.cpm_minor is None
    assert result.roas is None
    assert result.frequency is None
    assert result.hook_rate is None
    assert result.hold_rate is None
