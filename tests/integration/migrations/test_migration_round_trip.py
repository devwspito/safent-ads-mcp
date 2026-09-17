"""Ida y vuelta completa sobre una base recien creada: head -> base -> head.
Un downgrade que no funciona es un downgrade que no existe, y el orden de
las revisiones se demuestra corriendolo, no leyendolo."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from testcontainers.community.postgres import PostgresContainer

from tests.integration.migrations.conftest import (
    downgrade,
    to_alembic_dsn,
    to_asyncpg_dsn,
    upgrade,
    with_database,
)

pytestmark = pytest.mark.integration

_ROUND_TRIP_DB = "ads_round_trip"

_EXPECTED_TABLES = {
    "businesses",
    "platform_accounts",
    "credential_refs",
    "owners",
    "sessions",
    "login_attempts",
    "decision_log",
    "ad_entities",
    "metrics_daily",
    "metrics_hourly",
    "metrics_restatements",
    "data_freshness",
    "offerings",
    "calendar_events",
    "lead_attributions",
    "signals",
    "anomalies",
    "rules",
    "guardrails",
    "emergency_brakes",
    "rule_firings",
    "proposals",
    "approvals",
    "executions",
    "spend_ledger",
    "execution_reservations",
    "notifications",
    "telegram_callbacks",
    "brand_kits",
    "oauth_connect_sessions",
    "owner_preferences",
}


async def _tables(dsn: str) -> set[str]:
    connection = await asyncpg.connect(dsn)
    try:
        rows = await connection.fetch(
            """
            SELECT c.relname AS name
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
            """
        )
        return {row["name"] for row in rows}
    finally:
        await connection.close()


async def _seeded_rules(dsn: str) -> int:
    connection = await asyncpg.connect(dsn)
    try:
        return await connection.fetchval("SELECT count(*) FROM rules WHERE scope = 'global'")
    finally:
        await connection.close()


async def test_upgrade_downgrade_upgrade(postgres_container: PostgresContainer) -> None:
    admin_dsn = to_asyncpg_dsn(postgres_container.get_connection_url())
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute("DROP DATABASE IF EXISTS ads_round_trip")
        await admin.execute("CREATE DATABASE ads_round_trip")
    finally:
        await admin.close()

    alembic_dsn = with_database(
        to_alembic_dsn(postgres_container.get_connection_url()), _ROUND_TRIP_DB
    )
    query_dsn = with_database(admin_dsn, _ROUND_TRIP_DB)

    await asyncio.to_thread(upgrade, alembic_dsn)
    assert _EXPECTED_TABLES <= await _tables(query_dsn)
    assert await _seeded_rules(query_dsn) == 37

    await asyncio.to_thread(downgrade, alembic_dsn)
    remaining = await _tables(query_dsn)
    assert remaining & _EXPECTED_TABLES == set()
    assert "alembic_version" in remaining

    await asyncio.to_thread(upgrade, alembic_dsn)
    assert _EXPECTED_TABLES <= await _tables(query_dsn)
    assert await _seeded_rules(query_dsn) == 37
