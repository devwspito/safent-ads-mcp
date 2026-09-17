"""`LiveEconomicsStep` (T156) contra Postgres real: una vuelta llena las
tres tablas de `0016_economics` para un negocio con producto activo, CRM y
metricas reales -- y una segunda vuelta el mismo dia no duplica el perfil
(idempotencia)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import MappingProxyType

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import (
    AttributionRung,
    LeadAttribution,
    LeadAttributionId,
)
from safent_ads.crm.infrastructure.sql_repositories import SqlLeadAttributionRepository
from safent_ads.metrics.domain.conversion_kind import ConversionKind as MetricsConversionKind
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.infrastructure.sql_repositories import SqlMetricFactRepository
from safent_ads.orchestration.infrastructure.economics_step import LiveEconomicsStep
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_AS_OF = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture
async def session_factory(
    database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def seeded_business_with_offering(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[uuid.UUID, EntityRef, uuid.UUID, uuid.UUID]]:
    entity_ref = campaign_ref(f"eco{uuid.uuid4().hex[:8]}", platform_value="google")
    offering_id = uuid.uuid4()
    calendar_event_id = uuid.uuid4()
    async with session_factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await session.execute(
            text(
                "INSERT INTO offerings (id, business_id, code, title, price_amount, "
                "price_currency) VALUES (:id, :business_id, :code, 'Oferta ciclo', 1200, 'EUR')"
            ),
            {"id": offering_id, "business_id": business_id, "code": f"off-{offering_id.hex[:10]}"},
        )
        await session.execute(
            text(
                "INSERT INTO calendar_events (id, business_id, offering_id, name, "
                "window_start, window_end, source) "
                "VALUES (:id, :business_id, :offering_id, 'Evento ciclo', '2026-01-01', "
                "'2026-06-01', 'test')"
            ),
            {"id": calendar_event_id, "business_id": business_id, "offering_id": offering_id},
        )
        await session.commit()
    try:
        yield business_id, entity_ref, offering_id, calendar_event_id
    finally:
        # `unit_economics_profiles` es append-only (trigger de
        # `0016_economics`): este test crea al menos una fila, y esa fila
        # bloquea con `ON DELETE RESTRICT` cualquier limpieza posterior de
        # `businesses`. Coherente con el diseno ("nunca reescribe la
        # vigente"): el negocio de prueba queda, a proposito, sin purgar
        # del Postgres efimero de la sesion de tests -- mismo criterio que
        # produccion, donde tampoco se borra jamas.
        async with session_factory() as session:
            params = {"id": business_id}
            await session.execute(
                text("DELETE FROM lag_curve_snapshots WHERE business_id = :id"), params
            )
            await session.execute(
                text("DELETE FROM platform_divergence_snapshots WHERE business_id = :id"), params
            )
            await session.execute(
                text("DELETE FROM lead_attributions WHERE business_id = :id"), params
            )
            await session.execute(text("DELETE FROM metrics_daily WHERE business_id = :id"), params)
            await session.commit()


async def test_a_single_pass_fills_the_three_economics_tables(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_business_with_offering: tuple[uuid.UUID, EntityRef, uuid.UUID, uuid.UUID],
) -> None:
    business_id, entity_ref, offering_id, calendar_event_id = seeded_business_with_offering
    typed_business_id = BusinessId(business_id)

    async with session_factory() as session:
        identity = HashedIdentity.compute(
            business_id=typed_business_id, raw_identifier="a@x.com", salt="s"
        )
        attributions = SqlLeadAttributionRepository(session)
        await attributions.save(
            LeadAttribution(
                lead_attribution_id=LeadAttributionId.new(),
                business_id=typed_business_id,
                hashed_identity=identity,
                entity_ref=entity_ref,
                attribution_rung=AttributionRung.HASHED_IDENTITY,
                conversion_kind=ConversionKind.LEAD,
                value_minor=0,
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                calendar_event_id=str(calendar_event_id),
            )
        )
        await attributions.save(
            LeadAttribution(
                lead_attribution_id=LeadAttributionId.new(),
                business_id=typed_business_id,
                hashed_identity=identity,
                entity_ref=entity_ref,
                attribution_rung=AttributionRung.HASHED_IDENTITY,
                conversion_kind=ConversionKind.BUSINESS_CONVERSION,
                value_minor=110_000,
                occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
                observed_at=datetime(2026, 1, 10, tzinfo=UTC),
                calendar_event_id=str(calendar_event_id),
            )
        )
        await SqlMetricFactRepository(session).upsert_many(
            [
                MetricFact(
                    entity_ref=entity_ref,
                    stat_date=datetime(2026, 2, 1, tzinfo=UTC).date(),
                    account_timezone="Europe/Madrid",
                    currency="EUR",
                    spend_minor=50_000,
                    impressions=5_000,
                    clicks=300,
                    reach=2_500,
                    conversions=MappingProxyType({MetricsConversionKind.BUSINESS_CONVERSION: 2}),
                    ingested_at=datetime(2026, 2, 1, tzinfo=UTC),
                )
            ]
        )
        await session.commit()

    step = LiveEconomicsStep(session_factory, FixedClock(_AS_OF))
    await step.run(typed_business_id, "cycle-1", _AS_OF)

    async with session_factory() as session:
        profiles = await session.execute(
            text("SELECT count(*) FROM unit_economics_profiles WHERE business_id = :b"),
            {"b": business_id},
        )
        assert profiles.scalar_one() == 1

        curves = await session.execute(
            text(
                "SELECT count(*) FROM lag_curve_snapshots "
                "WHERE business_id = :b AND product_id = :p AND platform = 'google'"
            ),
            {"b": business_id, "p": offering_id},
        )
        assert curves.scalar_one() == 1

        divergences = await session.execute(
            text("SELECT count(*) FROM platform_divergence_snapshots WHERE business_id = :b"),
            {"b": business_id},
        )
        assert divergences.scalar_one() == 1

    # Segunda vuelta el mismo dia: idempotente, no duplica el perfil.
    await step.run(typed_business_id, "cycle-2", _AS_OF)
    async with session_factory() as session:
        profiles_again = await session.execute(
            text("SELECT count(*) FROM unit_economics_profiles WHERE business_id = :b"),
            {"b": business_id},
        )
        assert profiles_again.scalar_one() == 1
