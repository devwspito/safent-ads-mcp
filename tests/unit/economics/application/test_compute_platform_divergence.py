"""`ComputePlatformDivergence` (T156, profitability-engine.md §2): `delta_hat`
sobre las 8 semanas cerradas -- CRM contra plataforma para las entidades de
una cuenta."""

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
from safent_ads.economics.application.compute_platform_divergence import ComputePlatformDivergence
from safent_ads.economics.testing.in_memory_repositories import (
    InMemoryPlatformDivergenceRepository,
)
from safent_ads.metrics.domain.conversion_kind import ConversionKind as MetricsConversionKind
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.testing.in_memory_metric_fact_repository import (
    InMemoryMetricFactRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = BusinessId.new()
_ENTITY_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")
_PLATFORM_ACCOUNT_ID = "acc-1"
# Domingo: el lunes anterior (2026-02-16) es el cierre de la semana en curso.
_NOW = datetime(2026, 3, 1, tzinfo=UTC)


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


async def test_without_entity_refs_nothing_is_computed() -> None:
    use_case = ComputePlatformDivergence(
        InMemoryLeadAttributionRepository(),
        InMemoryMetricFactRepository(),
        InMemoryPlatformDivergenceRepository(),
        FixedClock(_NOW),
    )

    result = await use_case.execute(
        business_id=_BUSINESS_ID, platform_account_id=_PLATFORM_ACCOUNT_ID, entity_refs=[]
    )

    assert result is None


async def test_computes_divergence_from_crm_and_platform_counts() -> None:
    lead_attributions = InMemoryLeadAttributionRepository()
    await lead_attributions.save(_crm_conversion("a@x.com", datetime(2026, 2, 1, tzinfo=UTC)))
    await lead_attributions.save(_crm_conversion("b@x.com", datetime(2026, 2, 2, tzinfo=UTC)))

    metrics = InMemoryMetricFactRepository()
    await metrics.upsert_many(
        [
            _metric_fact(datetime(2026, 2, 1, tzinfo=UTC), conversions=3),
            _metric_fact(datetime(2026, 2, 2, tzinfo=UTC), conversions=3),
        ]
    )

    divergences = InMemoryPlatformDivergenceRepository()
    use_case = ComputePlatformDivergence(lead_attributions, metrics, divergences, FixedClock(_NOW))

    divergence = await use_case.execute(
        business_id=_BUSINESS_ID,
        platform_account_id=_PLATFORM_ACCOUNT_ID,
        entity_refs=[_ENTITY_REF],
    )

    assert divergence is not None
    assert divergence.crm_conversions == 2
    assert divergence.platform_conversions == 6
    stored = await divergences.get_latest(
        business_id=_BUSINESS_ID, platform_account_id=_PLATFORM_ACCOUNT_ID
    )
    assert stored == divergence


async def test_the_current_unclosed_week_never_counts() -> None:
    """`_NOW` es 2026-03-01 (domingo); el lunes de esa semana (2026-02-23)
    es el corte EXCLUSIVO -- un evento de esa semana en curso no cuenta
    todavia en ninguno de los dos lados."""
    lead_attributions = InMemoryLeadAttributionRepository()
    await lead_attributions.save(_crm_conversion("c@x.com", datetime(2026, 2, 24, tzinfo=UTC)))

    metrics = InMemoryMetricFactRepository()
    await metrics.upsert_many([_metric_fact(datetime(2026, 2, 24, tzinfo=UTC), conversions=5)])

    use_case = ComputePlatformDivergence(
        lead_attributions, metrics, InMemoryPlatformDivergenceRepository(), FixedClock(_NOW)
    )

    divergence = await use_case.execute(
        business_id=_BUSINESS_ID,
        platform_account_id=_PLATFORM_ACCOUNT_ID,
        entity_refs=[_ENTITY_REF],
    )

    assert divergence is not None
    assert divergence.crm_conversions == 0
    assert divergence.platform_conversions == 0
