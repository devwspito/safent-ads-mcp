"""`PortfolioReadPort`: `FakePortfolioReadPort` (`mcp/testing/fakes.py`)
contra `SqlPortfolioReadPort` (esta lane). Los importes exactos difieren a
proposito (el fake devuelve cifras de muestra fijas; SQL sale de
`metrics_daily`/`guardrails` sembrados de verdad) -- el banco compartido
comprueba forma y presencia; la suma con gasto sembrado a mano vive en un
test SQL-only, mismo patron que
`tests/integration/panel/test_sql_read_model_integration.py`."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

from safent_ads.mcp.application.dto import Window, WindowPreset
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.sql_portfolio_read_port import SqlPortfolioReadPort
from safent_ads.mcp.testing.fakes import BUSINESS_A, FakePortfolioReadPort
from safent_ads.shared.clock import FixedClock

_TODAY = date(2026, 3, 15)
_NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
_WINDOW = Window(preset=WindowPreset.SEVEN_DAYS, lag_days=0, date_from=None, date_to=None)
_TODAY_SPEND_MINOR = 5_000
_EARLIER_SPEND_MINOR = 3_000


@dataclass(slots=True)
class PortfolioFixture:
    port: object
    business_id: str


async def _insert_daily_fact(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    entity_ref: str,
    stat_date: date,
    spend_minor: int,
) -> None:
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
                 account_timezone, currency, spend, impressions, clicks, conversions_lead,
                 conversion_value, ingested_at)
            VALUES
                (:business_id, :entity_ref, 'campaign', :account_id, :stat_date,
                 'Europe/Madrid', 'EUR', :spend, 100, 10, 2, 0, :ingested_at)
        """),
        {
            "business_id": business_id,
            "entity_ref": entity_ref,
            "account_id": account_id,
            "stat_date": stat_date,
            "spend": spend_minor,
            "ingested_at": _NOW - timedelta(minutes=10),
        },
    )


async def _seed_sql_portfolio(
    factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    entity_ref = campaign_ref(f"pf{uuid.uuid4().hex[:8]}", platform_value="google")
    async with factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await _insert_daily_fact(
            session,
            business_id=business_id,
            entity_ref=str(entity_ref),
            stat_date=_TODAY,
            spend_minor=_TODAY_SPEND_MINOR,
        )
        await _insert_daily_fact(
            session,
            business_id=business_id,
            entity_ref=str(entity_ref),
            stat_date=_TODAY - timedelta(days=3),
            spend_minor=_EARLIER_SPEND_MINOR,
        )
        await session.commit()
    return business_id


@pytest.fixture(
    params=[
        pytest.param("fake", id="fake"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def portfolio(request: pytest.FixtureRequest) -> AsyncIterator[PortfolioFixture]:
    if request.param == "fake":
        yield PortfolioFixture(FakePortfolioReadPort(), BUSINESS_A)
        return

    factory: async_sessionmaker[AsyncSession] = request.getfixturevalue("mcp_session_factory")
    business_id = await _seed_sql_portfolio(factory)
    port = SqlPortfolioReadPort(factory, clock=FixedClock(_NOW))
    yield PortfolioFixture(port, str(business_id))


async def test_list_platform_accounts_returns_at_least_one_account(
    portfolio: PortfolioFixture,
) -> None:
    accounts = await portfolio.port.list_platform_accounts(portfolio.business_id)

    assert len(accounts) >= 1


async def test_get_portfolio_overview_reports_spend_and_freshness(
    portfolio: PortfolioFixture,
) -> None:
    overview = await portfolio.port.get_portfolio_overview(portfolio.business_id, _WINDOW)

    assert overview.spend.window.amount >= 0
    assert overview.freshness.last_ingested_at is not None
    assert isinstance(overview.is_partial, bool)


async def test_get_data_freshness_returns_at_least_one_account(
    portfolio: PortfolioFixture,
) -> None:
    freshness = await portfolio.port.get_data_freshness(portfolio.business_id)

    assert len(freshness) >= 1


@pytest.mark.integration
async def test_sql_portfolio_spend_matches_hand_computed_sums(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_id = await _seed_sql_portfolio(mcp_session_factory)
    port = SqlPortfolioReadPort(mcp_session_factory, clock=FixedClock(_NOW))

    overview = await port.get_portfolio_overview(str(business_id), _WINDOW)

    assert overview.spend.today.amount == Decimal(_TODAY_SPEND_MINOR) / 100
    assert overview.spend.window.amount == Decimal(
        _TODAY_SPEND_MINOR + _EARLIER_SPEND_MINOR
    ) / 100
    assert overview.is_partial is True  # sin guardarrail sembrado


@pytest.mark.integration
async def test_sql_get_portfolio_overview_unknown_business_raises_not_found(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    port = SqlPortfolioReadPort(mcp_session_factory, clock=FixedClock(_NOW))

    with pytest.raises(EntityNotFoundError):
        await port.get_portfolio_overview(str(uuid.uuid4()), _WINDOW)
