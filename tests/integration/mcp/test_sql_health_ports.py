"""`SqlAccountLinkStatusPort`/`SqlDatabaseHealthPort` (mcp/infrastructure,
`GET /mcp/health`): mismo patron de sesion propia por llamada que
`sql_brand_read_port` (ver su docstring) -- semillan y confirman con su
propio motor, nunca con `rolled_back_session` (quedaria invisible para la
sesion que el puerto abre)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from safent_ads.mcp.infrastructure.sql_health_ports import (
    SqlAccountLinkStatusPort,
    SqlDatabaseHealthPort,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def committed_google_account(
    isolated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    business_id = uuid.uuid4()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Health Fixture', 'Europe/Madrid', 'EUR')"
            ),
            {"id": str(business_id), "slug": f"health-fixture-{business_id.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO platform_accounts "
                "(business_id, platform, external_account_id, currency, timezone, "
                " api_tier, status) "
                "VALUES (:business_id, 'google', :external_id, 'EUR', 'Europe/Madrid', "
                " 'google_standard', 'ACTIVE')"
            ),
            {"business_id": str(business_id), "external_id": f"act_{business_id.hex[:8]}"},
        )
        await session.commit()
    try:
        yield factory
    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM platform_accounts WHERE business_id = :id"),
                {"id": str(business_id)},
            )
            await session.execute(
                text("DELETE FROM businesses WHERE id = :id"), {"id": str(business_id)}
            )
            await session.commit()
        await engine.dispose()


async def test_linked_platforms_reports_the_saved_account(
    committed_google_account: async_sessionmaker[AsyncSession],
) -> None:
    linked = await SqlAccountLinkStatusPort(committed_google_account).linked_platforms()

    # `linked_platforms()` es global por diseno (salud de toda la instalacion) y
    # la base se comparte entre tests: solo se afirma lo que este test insertó.
    assert "google" in linked
    assert set(linked) <= {"google", "meta"}


async def test_ping_succeeds_against_a_real_database(database_url: str) -> None:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        assert await SqlDatabaseHealthPort(factory).ping() is True
    finally:
        await engine.dispose()


async def test_ping_returns_false_when_database_is_unreachable() -> None:
    engine = create_async_engine(
        "postgresql+asyncpg://ads:wrong@127.0.0.1:1/does-not-exist", pool_pre_ping=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        assert await SqlDatabaseHealthPort(factory).ping() is False
    finally:
        await engine.dispose()
