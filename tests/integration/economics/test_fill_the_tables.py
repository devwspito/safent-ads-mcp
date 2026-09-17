"""T156 contra Postgres real: `BuildUnitEconomicsProfile`, `ComputeLagCurve`
y `ComputePlatformDivergence` llenando las tres tablas de `0016_economics`
desde `lead_attributions` (T145) y `metrics_daily` reales, con los
adaptadores SQL de produccion -- no dobles."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import (
    AttributionRung,
    LeadAttribution,
    LeadAttributionId,
)
from safent_ads.crm.infrastructure.sql_repositories import SqlLeadAttributionRepository
from safent_ads.economics.application.build_unit_economics_profile import (
    BuildUnitEconomicsProfile,
)
from safent_ads.economics.application.compute_lag_curve import ComputeLagCurve
from safent_ads.economics.application.compute_platform_divergence import ComputePlatformDivergence
from safent_ads.economics.application.ports import MarginInputs
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.economics.domain.unit_economics import ProfileStatus
from safent_ads.economics.infrastructure.crm_lag_observation_repository import (
    CrmLagObservationRepository,
)
from safent_ads.economics.infrastructure.sql_repositories import (
    SqlCalendarEventLookupPort,
    SqlLagCurveRepository,
    SqlOfferingPricePort,
    SqlPlatformDivergenceRepository,
    SqlUnitEconomicsProfileRepository,
)
from safent_ads.economics.testing.in_memory_repositories import InMemoryMarginInputsPort
from safent_ads.metrics.domain.conversion_kind import ConversionKind as MetricsConversionKind
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.infrastructure.sql_repositories import SqlMetricFactRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

pytestmark = pytest.mark.integration

_AS_OF = datetime(2026, 3, 1, tzinfo=UTC)  # domingo: cierre de semana el lunes 2026-02-23
_GOOGLE = PlatformCode.GOOGLE


async def _seed_business_account_and_entity(
    session: AsyncSession, *, external_id: str
) -> tuple[uuid.UUID, uuid.UUID, EntityRef]:
    business_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    account_id = uuid.uuid4()
    entity_ref = EntityRef(platform=_GOOGLE, level=EntityLevel.CAMPAIGN, external_id=external_id)
    await session.execute(
        text(
            "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
            "VALUES (:id, :slug, 'Negocio T156', 'Europe/Madrid', 'EUR')"
        ),
        {"id": business_id, "slug": f"t156-{business_id.hex[:12]}"},
    )
    await session.execute(
        text("INSERT INTO credential_refs (id, platform, alias) VALUES (:id, 'google', :alias)"),
        {"id": credential_id, "alias": f"alias-{business_id.hex[:12]}"},
    )
    await session.execute(
        text(
            "INSERT INTO platform_accounts (id, business_id, platform, external_account_id, "
            "currency, timezone, api_tier, credential_ref_id, status) "
            "VALUES (:id, :business_id, 'google', :external_account_id, 'EUR', "
            "'Europe/Madrid', 'google_standard', :credential_ref_id, 'ACTIVE')"
        ),
        {
            "id": account_id,
            "business_id": business_id,
            "external_account_id": f"act_{external_id}",
            "credential_ref_id": credential_id,
        },
    )
    await session.execute(
        text(
            "INSERT INTO ad_entities (business_id, platform_account_id, platform, level, "
            "external_id, name, status, platform_state_hash) "
            "VALUES (:business_id, :account_id, 'google', 'campaign', :external_id, "
            "'Campana T156', 'ACTIVE', :hash)"
        ),
        {
            "business_id": business_id,
            "account_id": account_id,
            "external_id": external_id,
            "hash": "a" * 64,
        },
    )
    await session.flush()
    return business_id, account_id, entity_ref


async def _seed_offering_and_calendar_event(
    session: AsyncSession, *, business_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    offering_id = uuid.uuid4()
    calendar_event_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO offerings (id, business_id, code, title, price_amount, price_currency) "
            "VALUES (:id, :business_id, :code, 'Oferta T156', 1200, 'EUR')"
        ),
        {"id": offering_id, "business_id": business_id, "code": f"off-{offering_id.hex[:10]}"},
    )
    await session.execute(
        text(
            "INSERT INTO calendar_events (id, business_id, offering_id, name, window_start, "
            "window_end, source) "
            "VALUES (:id, :business_id, :offering_id, 'Evento T156', '2026-01-01', "
            "'2026-06-01', 'test')"
        ),
        {"id": calendar_event_id, "business_id": business_id, "offering_id": offering_id},
    )
    await session.flush()
    return offering_id, calendar_event_id


def _identity(business_id: BusinessId, raw: str) -> HashedIdentity:
    return HashedIdentity.compute(business_id=business_id, raw_identifier=raw, salt="t156")


def _lead_attribution(
    *,
    business_id: BusinessId,
    entity_ref: EntityRef,
    calendar_event_id: str | None,
    raw_identity: str,
    kind: ConversionKind,
    occurred_at: datetime,
    value_minor: int = 0,
) -> LeadAttribution:
    return LeadAttribution(
        lead_attribution_id=LeadAttributionId.new(),
        business_id=business_id,
        hashed_identity=_identity(business_id, raw_identity),
        entity_ref=entity_ref,
        attribution_rung=AttributionRung.HASHED_IDENTITY,
        conversion_kind=kind,
        value_minor=value_minor,
        occurred_at=occurred_at,
        observed_at=occurred_at,
        calendar_event_id=calendar_event_id,
    )


def _metric_fact(entity_ref: EntityRef, stat_date: datetime, conversions: int) -> MetricFact:
    return MetricFact(
        entity_ref=entity_ref,
        stat_date=stat_date.date(),
        account_timezone="Europe/Madrid",
        currency="EUR",
        spend_minor=50_000,
        impressions=5_000,
        clicks=300,
        reach=2_500,
        conversions=MappingProxyType({MetricsConversionKind.BUSINESS_CONVERSION: conversions}),
        ingested_at=stat_date,
    )


async def test_build_unit_economics_profile_confirms_from_real_crm_history(
    db_session: AsyncSession,
) -> None:
    business_id, _account_id, entity_ref = await _seed_business_account_and_entity(
        db_session, external_id="ue-1"
    )
    offering_id, calendar_event_id = await _seed_offering_and_calendar_event(
        db_session, business_id=business_id
    )
    product_id = ProductId.parse(str(offering_id))
    typed_business_id = BusinessId(business_id)

    attributions = SqlLeadAttributionRepository(db_session)
    await attributions.save(
        _lead_attribution(
            business_id=typed_business_id,
            entity_ref=entity_ref,
            calendar_event_id=str(calendar_event_id),
            raw_identity="a@x.com",
            kind=ConversionKind.LEAD,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await attributions.save(
        _lead_attribution(
            business_id=typed_business_id,
            entity_ref=entity_ref,
            calendar_event_id=str(calendar_event_id),
            raw_identity="a@x.com",
            kind=ConversionKind.BUSINESS_CONVERSION,
            occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
            value_minor=110_000,
        )
    )

    margin_inputs = InMemoryMarginInputsPort()
    margin_inputs.seed(
        business_id=typed_business_id,
        product_id=product_id,
        margin_inputs=MarginInputs(
            vat_rate=Rate.zero(),
            delivery_cost=Money.of("90"),
            monthly_sales_team_cost=Money.of("140"),
            theta=Theta(Decimal("0.35")),
            margin_horizon_days=90,
        ),
    )

    use_case = BuildUnitEconomicsProfile(
        profiles=SqlUnitEconomicsProfileRepository(db_session),
        margin_inputs=margin_inputs,
        offering_prices=SqlOfferingPricePort(db_session),
        calendar_events=SqlCalendarEventLookupPort(db_session),
        lead_attributions=attributions,
        clock=FixedClock(_AS_OF),
    )

    profile = await use_case.execute(business_id=typed_business_id, product_id=product_id)

    assert profile.status is ProfileStatus.CONFIRMED
    assert profile.cvr_lead_to_business_conversion.value == Decimal("1")

    row = await db_session.execute(
        text("SELECT count(*) FROM unit_economics_profiles WHERE business_id = :b"),
        {"b": business_id},
    )
    assert row.scalar_one() == 1


async def test_compute_lag_curve_materializes_from_real_lead_attributions(
    db_session: AsyncSession,
) -> None:
    business_id, _account_id, entity_ref = await _seed_business_account_and_entity(
        db_session, external_id="lc-1"
    )
    offering_id, calendar_event_id = await _seed_offering_and_calendar_event(
        db_session, business_id=business_id
    )
    product_id = ProductId.parse(str(offering_id))
    typed_business_id = BusinessId(business_id)

    attributions = SqlLeadAttributionRepository(db_session)
    await attributions.save(
        _lead_attribution(
            business_id=typed_business_id,
            entity_ref=entity_ref,
            calendar_event_id=str(calendar_event_id),
            raw_identity="lead-1@x.com",
            kind=ConversionKind.LEAD,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await attributions.save(
        _lead_attribution(
            business_id=typed_business_id,
            entity_ref=entity_ref,
            calendar_event_id=str(calendar_event_id),
            raw_identity="lead-1@x.com",
            kind=ConversionKind.BUSINESS_CONVERSION,
            occurred_at=datetime(2026, 1, 15, tzinfo=UTC),
            value_minor=100_000,
        )
    )

    use_case = ComputeLagCurve(
        CrmLagObservationRepository(attributions, SqlCalendarEventLookupPort(db_session)),
        SqlLagCurveRepository(db_session),
        FixedClock(_AS_OF),
    )

    curve = await use_case.execute(
        business_id=typed_business_id, product_id=product_id, platform="google"
    )

    assert curve is not None
    assert curve.sample_size == 1
    assert curve.median_lag_days() == 14

    stored = await SqlLagCurveRepository(db_session).get_current(
        business_id=typed_business_id, product_id=product_id, platform="google"
    )
    assert stored is not None
    assert stored.sample_size == 1


async def test_compute_platform_divergence_from_real_crm_and_metrics(
    db_session: AsyncSession,
) -> None:
    business_id, account_id, entity_ref = await _seed_business_account_and_entity(
        db_session, external_id="pd-1"
    )
    typed_business_id = BusinessId(business_id)
    attributions = SqlLeadAttributionRepository(db_session)
    await attributions.save(
        _lead_attribution(
            business_id=typed_business_id,
            entity_ref=entity_ref,
            calendar_event_id=None,
            raw_identity="pd-a@x.com",
            kind=ConversionKind.BUSINESS_CONVERSION,
            occurred_at=datetime(2026, 2, 1, tzinfo=UTC),
            value_minor=100_000,
        )
    )

    metrics = SqlMetricFactRepository(db_session)
    await metrics.upsert_many(
        [_metric_fact(entity_ref, datetime(2026, 2, 1, tzinfo=UTC), conversions=3)]
    )

    use_case = ComputePlatformDivergence(
        attributions, metrics, SqlPlatformDivergenceRepository(db_session), FixedClock(_AS_OF)
    )

    divergence = await use_case.execute(
        business_id=typed_business_id,
        platform_account_id=str(account_id),
        entity_refs=[entity_ref],
    )

    assert divergence is not None
    assert divergence.crm_conversions == 1
    assert divergence.platform_conversions == 3

    stored = await SqlPlatformDivergenceRepository(db_session).get_latest(
        business_id=typed_business_id, platform_account_id=str(account_id)
    )
    assert stored is not None
    # `value` vive en `NUMERIC(8,4)` (0016_economics): redondeo esperado
    # frente al `float` calculado en memoria, no una divergencia real.
    assert stored.value == pytest.approx(divergence.value, abs=1e-4)
