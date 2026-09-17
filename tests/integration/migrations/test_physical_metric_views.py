"""Additive projections and scoped proposal validation preserve historical data."""

from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.proposals.infrastructure.value_codec import encode_value
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.integration.execution.test_physical_controls_sql import clone_connection
from tests.integration.migrations.conftest import downgrade, upgrade
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - fixture re-export
)
from tests.integration.orchestration.test_physical_observations import seed_metric

pytestmark = pytest.mark.integration
PREVIOUS = "0035_connection_identity"
CURRENT = "0036_physical_metric_views"


async def test_projection_roundtrip_never_rewrites_raw_facts(
    migration_sandbox: tuple[str, asyncpg.Connection],
) -> None:
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    engine = create_async_engine(dsn)
    try:
        async with AsyncSession(engine) as session:
            original = campaign_ref("migration-physical", "google")
            await seed_entity(session, original)
            other, _ = await clone_connection(session, original)
            await seed_metric(session, original, 200)
            await seed_metric(session, other, 200)
            await session.commit()
        before = await pg.fetch(
            "SELECT row_to_json(m)::text FROM metrics_daily m ORDER BY entity_ref"
        )
        upgrade(dsn, CURRENT)
        assert await pg.fetchval("SELECT SUM(spend) FROM metrics_daily_physical") == 200
        downgrade(dsn, PREVIOUS)
        assert await pg.fetchval("SELECT to_regclass('metrics_daily_physical')") is None
        assert (
            await pg.fetch("SELECT row_to_json(m)::text FROM metrics_daily m ORDER BY entity_ref")
            == before
        )
        upgrade(dsn, CURRENT)
        assert await pg.fetchval("SELECT SUM(spend) FROM metrics_daily_physical") == 200
    finally:
        await engine.dispose()


async def test_scoped_campaign_proposal_blocks_downgrade_without_altering_hash(
    migration_sandbox: tuple[str, asyncpg.Connection],
) -> None:
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    engine = create_async_engine(dsn)
    try:
        async with AsyncSession(engine) as session:
            original = campaign_ref("migration-opportunity", "google")
            business = BusinessId(await seed_entity(session, original))
            _, raw = await clone_connection(session, original)
            # This test freezes the 0036 schema; do not run today's repository
            # (which legitimately requires later additive columns) against it.
            after = {"brief": "Migration snapshot"}
            await session.execute(
                text("""INSERT INTO proposals
                (business_id,entity_ref,parameter,current_value,proposed_value,diff_hash,
                 classification,cause_key,cause,estimated_impact_amount,estimated_impact_currency,
                 urgency,expires_at)
                VALUES(:business,:ref,'new_campaign:migration',CAST(:before AS JSONB),
                       CAST(:after AS JSONB),:hash,'important','migration','Scope validation',
                       0,'EUR','recommended',now()+interval '1 day')"""),
                {
                    "business": business.value,
                    "ref": raw,
                    "before": encode_value(None),
                    "after": encode_value(after),
                    "hash": compute_diff_hash(
                        EntityRef.parse(raw), "new_campaign:migration", None, after
                    ),
                },
            )
            await session.commit()
        snapshot = await pg.fetchval("SELECT row_to_json(p)::text FROM proposals p")
        with pytest.raises(DBAPIError, match="Cannot downgrade with scoped account proposals"):
            downgrade(dsn, PREVIOUS)
        assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT
        assert await pg.fetchval("SELECT row_to_json(p)::text FROM proposals p") == snapshot
        assert await pg.fetchval("SELECT to_regclass('metrics_daily_physical')") is not None
        # Forging only the business still fails at the DB trust boundary.
        async with AsyncSession(engine) as session:
            with pytest.raises(DBAPIError, match="scope_mismatch"):
                await session.execute(
                    text("UPDATE proposals SET entity_ref=:ref"),
                    {"ref": raw.replace(str(business.value), str(uuid4()))},
                )
    finally:
        await engine.dispose()
