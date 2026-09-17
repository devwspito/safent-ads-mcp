"""`IngestHourlyMetrics`: exige `stat_hour` en todo el lote."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from safent_ads.metrics.application.errors import MixedBatchStatHourError
from safent_ads.metrics.application.ingest_hourly_metrics import (
    IngestHourlyMetrics,
    IngestHourlyMetricsRequest,
)
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.testing.in_memory_metric_fact_repository import (
    InMemoryMetricFactRepository,
)
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_ENTITY = EntityRef(platform=PlatformCode.META, level=EntityLevel.AD_SET, external_id="s1")


def _fact(*, stat_hour: int | None) -> MetricFact:
    return MetricFact(
        entity_ref=_ENTITY,
        stat_date=date(2026, 9, 1),
        stat_hour=stat_hour,
        account_timezone="Europe/Madrid",
        currency="EUR",
        spend_minor=100,
        impressions=10,
        clicks=1,
        reach=8,
        ingested_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


async def test_ingests_hourly_facts() -> None:
    repository = InMemoryMetricFactRepository()
    use_case = IngestHourlyMetrics(repository)

    await use_case.execute(IngestHourlyMetricsRequest(facts=[_fact(stat_hour=9)]))

    stored = await repository.find_by_natural_key(
        entity_ref=_ENTITY, stat_date=date(2026, 9, 1), stat_hour=9
    )
    assert stored is not None


async def test_rejects_daily_facts_in_hourly_batch() -> None:
    use_case = IngestHourlyMetrics(InMemoryMetricFactRepository())

    with pytest.raises(MixedBatchStatHourError):
        await use_case.execute(IngestHourlyMetricsRequest(facts=[_fact(stat_hour=None)]))
