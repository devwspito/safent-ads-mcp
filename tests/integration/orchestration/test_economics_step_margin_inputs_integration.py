"""`LiveEconomicsStep` con `offering_economics` rellena (T132): mismo
fixture que `test_economics_step_integration.py`, pero con
`SqlOfferingEconomicsRepository.upsert` ANTES del ciclo -- el perfil deja
de nacer `provisional` (profitability-engine.md §1: 'sin entradas del
dueno... provisional_from_price_only')."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

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
from safent_ads.economics.application.ports import OfferingEconomicsInput
from safent_ads.economics.domain.identifiers import ProductId
from safent_ads.economics.domain.payment_plan import PaymentPlan
from safent_ads.economics.domain.unit_economics import ProfileStatus
from safent_ads.economics.infrastructure.offering_economics_sql import (
    SqlOfferingEconomicsRepository,
)
from safent_ads.economics.infrastructure.sql_repositories import SqlUnitEconomicsProfileRepository
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
            await session.execute(
                text("DELETE FROM offering_economics WHERE business_id = :id"), params
            )
            await session.commit()


async def test_offering_economics_flips_the_profile_from_provisional_to_confirmed(
    session_factory: async_sessionmaker[AsyncSession],
    seeded_business_with_offering: tuple[uuid.UUID, EntityRef, uuid.UUID, uuid.UUID],
) -> None:
    business_id, entity_ref, offering_id, calendar_event_id = seeded_business_with_offering
    typed_business_id = BusinessId(business_id)
    typed_offering_id = ProductId(offering_id)

    async with session_factory() as session:
        await SqlOfferingEconomicsRepository(session).upsert(
            business_id=typed_business_id,
            offering_id=typed_offering_id,
            economics=OfferingEconomicsInput(
                vat_rate_pct=Decimal("0"),
                delivery_cost_minor=9_000,
                sales_cost_minor=14_000,
                refund_rate_pct=Decimal("6"),
                payment_plan=PaymentPlan.NONE,
                currency="EUR",
            ),
        )
        identity = HashedIdentity.compute(
            business_id=typed_business_id, raw_identifier="a@x.com", salt="s"
        )
        attributions = SqlLeadAttributionRepository(session)
        for kind, value_minor, occurred_at in (
            (ConversionKind.LEAD, 0, datetime(2026, 1, 1, tzinfo=UTC)),
            (ConversionKind.BUSINESS_CONVERSION, 110_000, datetime(2026, 1, 10, tzinfo=UTC)),
        ):
            await attributions.save(
                LeadAttribution(
                    lead_attribution_id=LeadAttributionId.new(),
                    business_id=typed_business_id,
                    hashed_identity=identity,
                    entity_ref=entity_ref,
                    attribution_rung=AttributionRung.HASHED_IDENTITY,
                    conversion_kind=kind,
                    value_minor=value_minor,
                    occurred_at=occurred_at,
                    observed_at=occurred_at,
                    calendar_event_id=str(calendar_event_id),
                )
            )
        await session.commit()

    step = LiveEconomicsStep(session_factory, FixedClock(_AS_OF))
    await step.run(typed_business_id, "cycle-1", _AS_OF)

    async with session_factory() as session:
        profile = await SqlUnitEconomicsProfileRepository(session).get_current(
            business_id=typed_business_id, product_id=typed_offering_id, as_of=_AS_OF.date()
        )

    assert profile is not None
    assert profile.status is ProfileStatus.CONFIRMED
