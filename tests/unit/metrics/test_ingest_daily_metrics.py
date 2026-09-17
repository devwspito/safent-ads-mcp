"""`IngestDailyMetrics`: UPSERT idempotente, rechaza facts horarios."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from safent_ads.metrics.application.errors import MixedBatchStatHourError
from safent_ads.metrics.application.ingest_daily_metrics import (
    IngestDailyMetrics,
    IngestDailyMetricsRequest,
)
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.testing.in_memory_metric_fact_repository import (
    InMemoryMetricFactRepository,
)
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_ENTITY = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")


def _fact(spend_minor: int, *, ingested_at: datetime) -> MetricFact:
    return MetricFact(
        entity_ref=_ENTITY,
        stat_date=date(2026, 9, 1),
        account_timezone="Europe/Madrid",
        currency="EUR",
        spend_minor=spend_minor,
        impressions=100,
        clicks=5,
        reach=90,
        ingested_at=ingested_at,
    )


async def test_ingest_upserts_by_natural_key() -> None:
    repository = InMemoryMetricFactRepository()
    use_case = IngestDailyMetrics(repository)

    first_ingest = datetime(2026, 9, 1, tzinfo=UTC)
    second_ingest = datetime(2026, 9, 2, tzinfo=UTC)
    await use_case.execute(
        IngestDailyMetricsRequest(facts=[_fact(1_000, ingested_at=first_ingest)])
    )
    await use_case.execute(
        IngestDailyMetricsRequest(facts=[_fact(1_500, ingested_at=second_ingest)])
    )

    stored = await repository.find_by_natural_key(
        entity_ref=_ENTITY, stat_date=date(2026, 9, 1), stat_hour=None
    )
    assert stored is not None
    assert stored.spend_minor == 1_500


async def test_rejects_hourly_facts_in_daily_batch() -> None:
    use_case = IngestDailyMetrics(InMemoryMetricFactRepository())
    hourly = MetricFact(
        entity_ref=_ENTITY,
        stat_date=date(2026, 9, 1),
        stat_hour=10,
        account_timezone="Europe/Madrid",
        currency="EUR",
        spend_minor=100,
        impressions=10,
        clicks=1,
        reach=8,
        ingested_at=datetime(2026, 9, 1, tzinfo=UTC),
    )

    with pytest.raises(MixedBatchStatHourError):
        await use_case.execute(IngestDailyMetricsRequest(facts=[hourly]))
