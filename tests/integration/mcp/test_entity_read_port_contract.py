"""`EntityReadPort`: `FakeEntityReadPort` (`mcp/testing/fakes.py`) contra
`SqlEntityReadPort` (esta lane). `list_creatives`/`get_creative` no se
prueban aqui: ambos quedan honestos-vacios en la implementacion SQL (sin
persistencia todavia, ver docstring del modulo) y coinciden trivialmente
con el fake solo por casualidad de forma, no de contrato real."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

from safent_ads.mcp.application.dto import Window, WindowPreset
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.sql_entity_read_port import SqlEntityReadPort
from safent_ads.mcp.testing.fakes import BUSINESS_A, FakeEntityReadPort
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
_WINDOW = Window(preset=WindowPreset.SEVEN_DAYS, lag_days=0, date_from=None, date_to=None)


@dataclass(slots=True)
class EntityFixture:
    port: object
    business_id: str
    entity_ref: str


async def _seed_sql_entity(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, str]:
    entity_ref = campaign_ref(f"ent{uuid.uuid4().hex[:8]}", platform_value="google")
    async with factory() as session:
        business_id = await seed_entity(session, entity_ref)
        account_id = (
            await session.execute(
                text("SELECT id FROM platform_accounts WHERE business_id = :business_id"),
                {"business_id": business_id},
            )
        ).scalar_one()
        await session.execute(
            text("""
                INSERT INTO metrics_daily
                    (business_id, entity_ref, entity_level, platform_account_id, stat_date,
                     account_timezone, currency, spend, impressions, clicks,
                     conversions_lead, conversion_value, ingested_at)
                VALUES
                    (:business_id, :entity_ref, 'campaign', :account_id, :stat_date,
                     'Europe/Madrid', 'EUR', 4200, 100, 10, 3, 0, :ingested_at)
            """),
            {
                "business_id": business_id,
                "entity_ref": str(entity_ref),
                "account_id": account_id,
                "stat_date": _NOW.date(),
                "ingested_at": _NOW - timedelta(minutes=5),
            },
        )
        await session.commit()
    return business_id, str(entity_ref)


@pytest.fixture(
    params=[
        pytest.param("fake", id="fake"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def entity(request: pytest.FixtureRequest) -> AsyncIterator[EntityFixture]:
    if request.param == "fake":
        fake = FakeEntityReadPort()
        page = await fake.list_campaigns(
            BUSINESS_A, platform=None, status=None, limit=10, cursor=None
        )
        yield EntityFixture(fake, BUSINESS_A, page.items[0].entity_ref)
        return

    factory: async_sessionmaker[AsyncSession] = request.getfixturevalue("mcp_session_factory")
    business_id, entity_ref = await _seed_sql_entity(factory)
    port = SqlEntityReadPort(factory, clock=FixedClock(_NOW))
    yield EntityFixture(port, str(business_id), entity_ref)


async def test_list_campaigns_includes_the_seeded_campaign(entity: EntityFixture) -> None:
    page = await entity.port.list_campaigns(
        entity.business_id, platform=None, status=None, limit=10, cursor=None
    )

    assert any(item.entity_ref == entity.entity_ref for item in page.items)


async def test_get_campaign_echoes_the_requested_ref(entity: EntityFixture) -> None:
    campaign = await entity.port.get_campaign(entity.business_id, entity.entity_ref)

    assert campaign.entity_ref == entity.entity_ref


async def test_get_entity_metrics_returns_a_series(entity: EntityFixture) -> None:
    series = await entity.port.get_entity_metrics(
        entity.business_id, entity.entity_ref, window=_WINDOW, granularity="daily"
    )

    assert series.entity_ref == entity.entity_ref
    assert isinstance(series.points, list)


async def test_get_insights_returns_a_breakdown_snapshot(entity: EntityFixture) -> None:
    snapshot = await entity.port.get_insights(
        entity.business_id, entity.entity_ref, window=_WINDOW, breakdown=None
    )

    assert snapshot.entity_ref == entity.entity_ref
    assert isinstance(snapshot.breakdown, dict)


@pytest.mark.integration
async def test_sql_get_campaign_unknown_ref_raises_not_found(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    port = SqlEntityReadPort(mcp_session_factory, clock=FixedClock(_NOW))

    with pytest.raises(EntityNotFoundError):
        await port.get_campaign(str(uuid.uuid4()), "google:campaign:does-not-exist")


@pytest.mark.integration
async def test_sql_get_campaign_rejects_entity_from_another_business(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _owner_business_id, entity_ref = await _seed_sql_entity(mcp_session_factory)
    port = SqlEntityReadPort(mcp_session_factory, clock=FixedClock(_NOW))

    with pytest.raises(EntityNotFoundError):
        await port.get_campaign(str(uuid.uuid4()), entity_ref)


@pytest.mark.integration
async def test_sql_get_entity_metrics_matches_hand_computed_spend(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_id, entity_ref = await _seed_sql_entity(mcp_session_factory)
    port = SqlEntityReadPort(mcp_session_factory, clock=FixedClock(_NOW))

    series = await port.get_entity_metrics(
        str(business_id), entity_ref, window=_WINDOW, granularity="daily"
    )

    assert len(series.points) == 1
    assert series.points[0].spend.amount == 42
    assert series.points[0].conversions == 3
