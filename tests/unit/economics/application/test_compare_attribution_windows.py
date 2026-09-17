"""`CompareAttributionWindows` (T160): misma entidad, tres ventanas
trailing (1/7/28 dias) terminando en `as_of`."""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import (
    AttributionRung,
    LeadAttribution,
    LeadAttributionId,
)
from safent_ads.crm.testing.in_memory_repositories import InMemoryLeadAttributionRepository
from safent_ads.economics.application.compare_attribution_windows import (
    CompareAttributionWindows,
)
from safent_ads.metrics.domain.conversion_kind import ConversionKind as MetricsConversionKind
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.testing.in_memory_metric_fact_repository import (
    InMemoryMetricFactRepository,
)
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = BusinessId.new()
_ENTITY_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")
_AS_OF = datetime(2026, 3, 1, tzinfo=UTC)


def _identity(raw: str) -> HashedIdentity:
    return HashedIdentity.compute(business_id=_BUSINESS_ID, raw_identifier=raw, salt="s")


def _crm_conversion(raw: str, occurred_at: datetime) -> LeadAttribution:
    return LeadAttribution(
        lead_attribution_id=LeadAttributionId.new(),
        business_id=_BUSINESS_ID,
        hashed_identity=_identity(raw),
        entity_ref=_ENTITY_REF,
        attribution_rung=AttributionRung.HASHED_IDENTITY,
        conversion_kind=ConversionKind.BUSINESS_CONVERSION,
        value_minor=100_000,
        occurred_at=occurred_at,
        observed_at=occurred_at,
    )


def _metric_fact(stat_date: datetime, conversions: int) -> MetricFact:
    return MetricFact(
        entity_ref=_ENTITY_REF,
        stat_date=stat_date.date(),
        account_timezone="Europe/Madrid",
        currency="EUR",
        spend_minor=10_000,
        impressions=1_000,
        clicks=100,
        reach=900,
        conversions=MappingProxyType({MetricsConversionKind.BUSINESS_CONVERSION: conversions}),
        ingested_at=stat_date,
    )


async def test_the_gap_narrows_as_the_window_widens() -> None:
    """Recien llegado (dentro del ultimo dia) el CRM aun no ha visto la
    conversion de hace 10 dias -- la ventana de 28d si la incluye."""
    lead_attributions = InMemoryLeadAttributionRepository()
    await lead_attributions.save(_crm_conversion("a@x.com", datetime(2026, 2, 19, tzinfo=UTC)))

    metrics = InMemoryMetricFactRepository()
    await metrics.upsert_many(
        [
            _metric_fact(datetime(2026, 2, 19, tzinfo=UTC), conversions=1),
            _metric_fact(datetime(2026, 2, 28, tzinfo=UTC), conversions=1),
        ]
    )

    use_case = CompareAttributionWindows(lead_attributions, metrics)
    rows = await use_case.execute(
        business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF, as_of=_AS_OF.date()
    )

    by_days = {row.window_days: row for row in rows}
    assert by_days[1].crm_conversions == 0
    assert by_days[1].platform_conversions == 1
    assert by_days[28].crm_conversions == 1
    assert by_days[28].platform_conversions == 2
    assert by_days[28].gap_pct < by_days[1].gap_pct
