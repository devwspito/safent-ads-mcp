"""`MetricFact`: contadores crudos, invariantes de negatividad y de
`stat_hour` (data-model.md §MetricFact)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import MappingProxyType

import pytest

from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.errors import InvalidStatHourError, NegativeCounterError
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_ENTITY = EntityRef(platform=PlatformCode.META, level=EntityLevel.AD_SET, external_id="123")
_NOW = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)


def _make(**overrides: object) -> MetricFact:
    defaults: dict[str, object] = {
        "entity_ref": _ENTITY,
        "stat_date": date(2026, 9, 8),
        "account_timezone": "Europe/Madrid",
        "currency": "EUR",
        "spend_minor": 5_000,
        "impressions": 1_000,
        "clicks": 20,
        "reach": 800,
        "ingested_at": _NOW,
    }
    defaults.update(overrides)
    return MetricFact(**defaults)  # type: ignore[arg-type]


def test_builds_a_valid_daily_fact() -> None:
    fact = _make()

    assert fact.is_hourly is False
    assert fact.natural_key == (_ENTITY, date(2026, 9, 8), None)


def test_builds_a_valid_hourly_fact() -> None:
    fact = _make(stat_hour=14)

    assert fact.is_hourly is True
    assert fact.natural_key == (_ENTITY, date(2026, 9, 8), 14)


@pytest.mark.parametrize("stat_hour", [-1, 24, 100])
def test_rejects_stat_hour_out_of_range(stat_hour: int) -> None:
    with pytest.raises(InvalidStatHourError):
        _make(stat_hour=stat_hour)


@pytest.mark.parametrize(
    "field", ["spend_minor", "impressions", "clicks", "reach", "video_views_3s"]
)
def test_rejects_negative_counters(field: str) -> None:
    with pytest.raises(NegativeCounterError):
        _make(**{field: -1})


def test_rejects_negative_conversion_count() -> None:
    with pytest.raises(NegativeCounterError):
        _make(conversions=MappingProxyType({ConversionKind.LEAD: -1}))


def test_conversions_of_and_total() -> None:
    fact = _make(
        conversions=MappingProxyType({ConversionKind.LEAD: 3, ConversionKind.CALL: 2})
    )

    assert fact.conversions_of(ConversionKind.LEAD) == 3
    assert fact.conversions_of(ConversionKind.WHATSAPP) == 0
    assert fact.total_conversions() == 5
