"""0004_metrics_facts: reingesta UPSERT, particionado mensual y
restatements solo-anexables."""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
import pytest

from tests.integration.migrations.conftest import (
    make_business,
    make_entity,
    make_platform_account,
)

pytestmark = pytest.mark.integration

_UPSERT_DAILY = """
    INSERT INTO metrics_daily (business_id, entity_ref, entity_level, platform_account_id,
                               stat_date, account_timezone, currency,
                               spend, impressions, clicks, conversions_lead)
    VALUES ($1, $2, 'campaign', $3, $4, 'Europe/Madrid', 'EUR', $5, $6, $7, $8)
    ON CONFLICT (entity_ref, stat_date) DO UPDATE
        SET spend             = EXCLUDED.spend,
            impressions       = EXCLUDED.impressions,
            clicks            = EXCLUDED.clicks,
            conversions_lead  = EXCLUDED.conversions_lead,
            revision          = metrics_daily.revision + 1,
            ingested_at       = now()
"""


async def _entity(pg: asyncpg.Connection) -> tuple[uuid.UUID, uuid.UUID, str]:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)
    return business_id, account_id, entity["entity_ref"]


async def test_upsert_no_duplicate_fact(pg: asyncpg.Connection) -> None:
    business_id, account_id, entity_ref = await _entity(pg)
    stat_date = dt.date(2026, 3, 14)

    await pg.execute(_UPSERT_DAILY, business_id, entity_ref, account_id, stat_date, 10, 100, 5, 1)
    await pg.execute(_UPSERT_DAILY, business_id, entity_ref, account_id, stat_date, 12, 130, 7, 2)
    await pg.execute(_UPSERT_DAILY, business_id, entity_ref, account_id, stat_date, 12, 130, 7, 2)

    row = await pg.fetchrow(
        """
        SELECT count(*) AS rows, max(spend) AS spend, max(revision) AS revision
        FROM metrics_daily WHERE entity_ref = $1 AND stat_date = $2
        """,
        entity_ref,
        stat_date,
    )

    assert row["rows"] == 1
    assert float(row["spend"]) == 12.0
    assert row["revision"] == 3


async def test_row_lands_in_its_month_partition(pg: asyncpg.Connection) -> None:
    business_id, account_id, entity_ref = await _entity(pg)
    stat_date = dt.date.today()
    await pg.execute(_UPSERT_DAILY, business_id, entity_ref, account_id, stat_date, 1, 1, 1, 0)

    partition = await pg.fetchval(
        "SELECT tableoid::regclass::text FROM metrics_daily WHERE entity_ref = $1", entity_ref
    )

    assert partition == f"metrics_daily_{stat_date:%Y_%m}"


async def test_unexpected_date_lands_in_the_default_partition(pg: asyncpg.Connection) -> None:
    business_id, account_id, entity_ref = await _entity(pg)
    far_future = dt.date.today() + dt.timedelta(days=3650)
    await pg.execute(_UPSERT_DAILY, business_id, entity_ref, account_id, far_future, 1, 1, 1, 0)

    partition = await pg.fetchval(
        "SELECT tableoid::regclass::text FROM metrics_daily WHERE entity_ref = $1", entity_ref
    )

    assert partition == "metrics_daily_default"


async def test_metrics_daily_rejects_unknown_entity(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pg.execute(
            _UPSERT_DAILY,
            business_id,
            "meta:campaign:no-existe",
            account_id,
            dt.date.today(),
            1,
            1,
            1,
            0,
        )


async def test_restatements_are_append_only(pg: asyncpg.Connection) -> None:
    business_id, _account_id, entity_ref = await _entity(pg)
    restatement_id = await pg.fetchval(
        """
        INSERT INTO metrics_restatements (business_id, entity_ref, stat_date, metric,
                                          old_value, new_value, reason, source, revision)
        VALUES ($1, $2, $3, 'conversions_lead', 3, 5, 'conversion tardia', 'crm', 2)
        RETURNING id
        """,
        business_id,
        entity_ref,
        dt.date(2026, 3, 14),
    )

    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await pg.execute(
            "UPDATE metrics_restatements SET new_value = 99 WHERE id = $1", restatement_id
        )
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await pg.execute("DELETE FROM metrics_restatements WHERE id = $1", restatement_id)


async def test_freshness_is_stale_is_derived(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)

    stale = await pg.fetchval(
        """
        INSERT INTO data_freshness (platform_account_id, entity_level, lag_minutes,
                                    stale_threshold_minutes)
        VALUES ($1, 'campaign', 90, 60)
        RETURNING is_stale
        """,
        account_id,
    )
    assert stale is True

    fresh = await pg.fetchval(
        """
        UPDATE data_freshness SET lag_minutes = 10
        WHERE platform_account_id = $1 AND entity_level = 'campaign'
        RETURNING is_stale
        """,
        account_id,
    )
    assert fresh is False


async def test_hourly_purge_keeps_the_window(pg: asyncpg.Connection) -> None:
    business_id, account_id, entity_ref = await _entity(pg)
    insert = """
        INSERT INTO metrics_hourly (business_id, entity_ref, platform_account_id, stat_date,
                                    stat_hour, account_timezone, currency, spend)
        VALUES ($1, $2, $3, $4, 9, 'Europe/Madrid', 'EUR', 1)
    """
    old_day = dt.date.today() - dt.timedelta(days=30)
    await pg.execute(insert, business_id, entity_ref, account_id, old_day)
    await pg.execute(insert, business_id, entity_ref, account_id, dt.date.today())

    await pg.fetchval("SELECT metrics_hourly_purge(14)")

    remaining = await pg.fetch(
        "SELECT stat_date FROM metrics_hourly WHERE entity_ref = $1", entity_ref
    )
    assert [row["stat_date"] for row in remaining] == [dt.date.today()]


async def test_ensure_partition_is_idempotent(pg: asyncpg.Connection) -> None:
    first = await pg.fetchval("SELECT metrics_daily_ensure_partition('2031-07-15'::date)")
    second = await pg.fetchval("SELECT metrics_daily_ensure_partition('2031-07-28'::date)")

    assert first == second == "metrics_daily_2031_07"
    exists = await pg.fetchval("SELECT to_regclass('metrics_daily_2031_07') IS NOT NULL")
    assert exists is True


async def test_retention_refuses_to_shrink_below_a_year(pg: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.RaiseError, match="retencion minima 12 meses"):
        await pg.fetchval("SELECT metrics_daily_drop_old_partitions(3)")
