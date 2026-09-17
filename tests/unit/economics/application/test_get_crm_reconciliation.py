"""`GetCrmReconciliation` (T160): plataforma vs CRM en una ventana."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import MappingProxyType

from safent_ads.crm.domain.bridge_health import ConnectorBridgeState, CrmBridgeHealth
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import (
    AttributionRung,
    LeadAttribution,
    LeadAttributionId,
)
from safent_ads.crm.testing.in_memory_repositories import (
    InMemoryCrmBridgeHealthRepository,
    InMemoryLeadAttributionRepository,
)
from safent_ads.economics.application.get_crm_reconciliation import GetCrmReconciliation
from safent_ads.metrics.domain.conversion_kind import ConversionKind as MetricsConversionKind
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.testing.in_memory_metric_fact_repository import (
    InMemoryMetricFactRepository,
)
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = BusinessId.new()
_ENTITY_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")


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


async def test_reports_platform_and_crm_conversions_with_gap() -> None:
    lead_attributions = InMemoryLeadAttributionRepository()
    await lead_attributions.save(_crm_conversion("a@x.com", datetime(2026, 2, 1, tzinfo=UTC)))

    metrics = InMemoryMetricFactRepository()
    await metrics.upsert_many([_metric_fact(datetime(2026, 2, 1, tzinfo=UTC), conversions=4)])

    use_case = GetCrmReconciliation(lead_attributions, metrics)
    view = await use_case.execute(
        business_id=_BUSINESS_ID,
        window_start=date(2026, 2, 1),
        window_end=date(2026, 2, 2),
        lag_days=3,
    )

    assert view.crm_conversions == 1
    assert view.platform_conversions == 4
    assert view.gap_pct == 0.75
    assert view.lag_days == 3


async def test_without_any_platform_conversions_the_gap_is_zero() -> None:
    use_case = GetCrmReconciliation(
        InMemoryLeadAttributionRepository(), InMemoryMetricFactRepository()
    )

    view = await use_case.execute(
        business_id=_BUSINESS_ID, window_start=date(2026, 2, 1), window_end=date(2026, 2, 2)
    )

    assert view.platform_conversions == 0
    assert view.crm_conversions == 0
    assert view.gap_pct == 0.0


async def test_customer_bridge_healthy_is_none_without_a_configured_bridge() -> None:
    use_case = GetCrmReconciliation(
        InMemoryLeadAttributionRepository(), InMemoryMetricFactRepository()
    )

    view = await use_case.execute(
        business_id=_BUSINESS_ID, window_start=date(2026, 2, 1), window_end=date(2026, 2, 2)
    )

    assert view.customer_bridge_healthy is None


async def test_customer_bridge_healthy_reflects_the_configured_bridge() -> None:
    bridge_health = InMemoryCrmBridgeHealthRepository()
    await bridge_health.upsert(
        CrmBridgeHealth.evaluate(
            business_id=_BUSINESS_ID,
            connector_id="connector-crm",
            connector_state=ConnectorBridgeState.DEGRADED,
            last_event_at=None,
            as_of=datetime(2026, 2, 1, tzinfo=UTC),
            cause="credential_expired",
        )
    )
    use_case = GetCrmReconciliation(
        InMemoryLeadAttributionRepository(),
        InMemoryMetricFactRepository(),
        crm_bridge_health=bridge_health,
    )

    view = await use_case.execute(
        business_id=_BUSINESS_ID, window_start=date(2026, 2, 1), window_end=date(2026, 2, 2)
    )

    assert view.customer_bridge_healthy is False
