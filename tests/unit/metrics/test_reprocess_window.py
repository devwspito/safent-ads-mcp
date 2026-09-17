"""`ReprocessWindow`: reproceso de 28 dias, UPSERT idempotente, `Restatement`
solo cuando el valor corregido difiere del almacenado (FR-5, NFR-6, NFR-7)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import MappingProxyType

from safent_ads.metrics.application.ingest_daily_metrics import (
    IngestDailyMetrics,
    IngestDailyMetricsRequest,
)
from safent_ads.metrics.application.reprocess_window import (
    ReprocessWindow,
    ReprocessWindowRequest,
)
from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.testing.in_memory_metric_fact_repository import (
    InMemoryMetricFactRepository,
)
from safent_ads.metrics.testing.in_memory_restatement_repository import (
    InMemoryRestatementRepository,
)
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_ENTITY = EntityRef(platform=PlatformCode.META, level=EntityLevel.AD, external_id="ad-1")
_INGESTED_AT = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
_RECORDED_AT = datetime(2026, 9, 9, 6, 0, tzinfo=UTC)


def _fact(
    *, conversions_business_conversion: int, ingested_at: datetime = _INGESTED_AT
) -> MetricFact:
    return MetricFact(
        entity_ref=_ENTITY,
        stat_date=date(2026, 8, 25),
        account_timezone="Europe/Madrid",
        currency="EUR",
        spend_minor=8_000,
        impressions=1_000,
        clicks=50,
        reach=900,
        conversions=MappingProxyType(
            {ConversionKind.BUSINESS_CONVERSION: conversions_business_conversion}
        ),
        ingested_at=ingested_at,
    )


async def _setup() -> tuple[
    InMemoryMetricFactRepository, InMemoryRestatementRepository, ReprocessWindow
]:
    facts_repo = InMemoryMetricFactRepository()
    restatements_repo = InMemoryRestatementRepository()
    await IngestDailyMetrics(facts_repo).execute(
        IngestDailyMetricsRequest(facts=[_fact(conversions_business_conversion=1)])
    )
    return facts_repo, restatements_repo, ReprocessWindow(facts_repo, restatements_repo)


async def test_reprocess_records_restatement_when_value_changes() -> None:
    facts_repo, restatements_repo, use_case = await _setup()
    corrected = _fact(conversions_business_conversion=3, ingested_at=_RECORDED_AT)

    produced = await use_case.execute(
        ReprocessWindowRequest(
            corrected_facts=[corrected], reason="conversion tardia CRM", recorded_at=_RECORDED_AT
        )
    )

    assert len(produced) == 1
    assert produced[0].field_name == f"conversions.{ConversionKind.BUSINESS_CONVERSION}"
    assert produced[0].old_value == 1
    assert produced[0].new_value == 3
    stored = await facts_repo.find_by_natural_key(
        entity_ref=_ENTITY, stat_date=date(2026, 8, 25), stat_hour=None
    )
    assert stored is not None
    assert stored.conversions_of(ConversionKind.BUSINESS_CONVERSION) == 3


async def test_reprocess_is_idempotent() -> None:
    facts_repo, restatements_repo, use_case = await _setup()
    corrected = _fact(conversions_business_conversion=3, ingested_at=_RECORDED_AT)
    request = ReprocessWindowRequest(
        corrected_facts=[corrected], reason="conversion tardia CRM", recorded_at=_RECORDED_AT
    )

    first_run = await use_case.execute(request)
    second_run = await use_case.execute(request)

    assert len(first_run) == 1
    assert len(second_run) == 0
    all_restatements = await restatements_repo.list_for_entity(entity_ref=_ENTITY)
    assert len(all_restatements) == 1
    stored = await facts_repo.find_by_natural_key(
        entity_ref=_ENTITY, stat_date=date(2026, 8, 25), stat_hour=None
    )
    assert stored is not None
    assert stored.conversions_of(ConversionKind.BUSINESS_CONVERSION) == 3
