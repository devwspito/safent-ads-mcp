"""`ComputeFreshness`: toma el `ingested_at` mas reciente entre entidades de
una cuenta+nivel (NFR-1)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from safent_ads.metrics.application.compute_freshness import (
    ComputeFreshness,
    ComputeFreshnessRequest,
)
from safent_ads.metrics.application.errors import NoMetricsIngestedError
from safent_ads.metrics.application.ingest_daily_metrics import (
    IngestDailyMetrics,
    IngestDailyMetricsRequest,
)
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.testing.in_memory_metric_fact_repository import (
    InMemoryMetricFactRepository,
)
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_ENTITY_A = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="a")
_ENTITY_B = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="b")


def _fact(entity_ref: EntityRef, *, ingested_at: datetime) -> MetricFact:
    return MetricFact(
        entity_ref=entity_ref,
        stat_date=date(2026, 9, 9),
        account_timezone="Europe/Madrid",
        currency="EUR",
        spend_minor=100,
        impressions=10,
        clicks=1,
        reach=8,
        ingested_at=ingested_at,
    )


async def test_takes_latest_ingested_at_across_entities() -> None:
    repository = InMemoryMetricFactRepository()
    await IngestDailyMetrics(repository).execute(
        IngestDailyMetricsRequest(
            facts=[
                _fact(_ENTITY_A, ingested_at=datetime(2026, 9, 9, 10, 0, tzinfo=UTC)),
                _fact(_ENTITY_B, ingested_at=datetime(2026, 9, 9, 10, 30, tzinfo=UTC)),
            ]
        )
    )
    use_case = ComputeFreshness(repository)

    freshness = await use_case.execute(
        ComputeFreshnessRequest(
            platform_account_ref="google:999",
            entity_level=EntityLevel.CAMPAIGN,
            entity_refs=[_ENTITY_A, _ENTITY_B],
            now=datetime(2026, 9, 9, 11, 0, tzinfo=UTC),
        )
    )

    assert freshness.last_ingested_at == datetime(2026, 9, 9, 10, 30, tzinfo=UTC)
    assert freshness.lag_minutes(now=datetime(2026, 9, 9, 11, 0, tzinfo=UTC)) == 30
    now = datetime(2026, 9, 9, 11, 0, tzinfo=UTC)
    assert freshness.is_stale(now=now, threshold_minutes=60) is False


async def test_raises_when_no_metrics_ingested_yet() -> None:
    use_case = ComputeFreshness(InMemoryMetricFactRepository())

    with pytest.raises(NoMetricsIngestedError):
        await use_case.execute(
            ComputeFreshnessRequest(
                platform_account_ref="google:999",
                entity_level=EntityLevel.CAMPAIGN,
                entity_refs=[_ENTITY_A],
                now=datetime(2026, 9, 9, 11, 0, tzinfo=UTC),
            )
        )
