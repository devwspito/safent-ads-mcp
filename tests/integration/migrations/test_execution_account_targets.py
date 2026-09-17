"""Typed target FKs retain signed history; account history forbids unsafe rollback."""

import uuid

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError

from tests.integration.migrations.conftest import (
    downgrade,
    make_approval,
    make_business,
    make_entity,
    make_platform_account,
    make_proposal,
    upgrade,
)
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - fixture re-export
)

pytestmark = pytest.mark.integration
PREVIOUS = "0036_physical_metric_views"
CURRENT = "0037_execution_account_targets"


async def seed_execution(pg, *, account_target=False, force_prior_hash=False):
    business = await make_business(pg)
    account = await make_platform_account(pg, business)
    if account_target:
        external = await pg.fetchval(
            "SELECT external_account_id FROM platform_accounts WHERE id=$1", account
        )
        ref = f"meta:account:{external}"
        parameter = "new_campaign:migration"
        state = "1" * 64 if force_prior_hash else None
    else:
        entity = await make_entity(pg, business, account)
        ref, parameter, state = entity["entity_ref"], "daily_budget", "1" * 64
    proposal = await make_proposal(pg, business, ref, parameter=parameter)
    approval = await make_approval(pg, proposal["id"], proposal["diff_hash"])
    execution = uuid.uuid4()
    await pg.execute(
        """INSERT INTO executions
        (id,business_id,proposal_id,authorization_id,entity_ref,idempotency_key,previous_value,platform_state_hash_before)
        VALUES($1,$2,$3,$4,$5,$6,'null'::jsonb,$7)""",
        execution,
        business,
        proposal["id"],
        approval,
        ref,
        f"exec-{execution}-{'a' * 12}",
        state,
    )
    return execution, business, account, ref


async def test_previous_schema_cannot_queue_real_account_creation(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    with pytest.raises(asyncpg.ForeignKeyViolationError, match="executions_entity_fk"):
        await seed_execution(pg, account_target=True, force_prior_hash=True)
    assert await pg.fetchval("SELECT count(*) FROM ad_entities") == 0


async def test_migration_preserves_signed_history_and_old_target_fk(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    execution, _, _, _ = await seed_execution(pg)
    snapshot = await pg.fetch(
        "SELECT entity_ref,diff_hash,current_value,proposed_value FROM proposals"
    )
    approvals = await pg.fetch("SELECT row_to_json(a)::text FROM approvals a")
    executions = await pg.fetch("SELECT row_to_json(e)::text FROM executions e")
    constraints_query = """SELECT conname, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint WHERE conrelid IN
            ('executions'::regclass,'execution_reservations'::regclass,'spend_ledger'::regclass)
        AND contype='f' AND conparentid=0 ORDER BY conname"""
    constraints = await pg.fetch(constraints_query)
    upgrade(dsn, CURRENT)
    assert await pg.fetchval(
        "SELECT target_ad_entity_ref=entity_ref FROM executions WHERE id=$1", execution
    )
    downgrade(dsn, PREVIOUS)
    assert (
        await pg.fetch("SELECT entity_ref,diff_hash,current_value,proposed_value FROM proposals")
        == snapshot
    )
    assert await pg.fetch("SELECT row_to_json(a)::text FROM approvals a") == approvals
    assert await pg.fetch("SELECT row_to_json(e)::text FROM executions e") == executions
    assert await pg.fetch(constraints_query) == constraints
    upgrade(dsn, CURRENT)
    assert await pg.fetchval("SELECT count(*) FROM executions") == 1


async def test_real_account_fk_and_scope_checks_downgrade_failclosed(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    execution, business, account, ref = await seed_execution(pg, account_target=True)
    assert await pg.fetchval("SELECT count(*) FROM ad_entities") == 0
    assert (
        await pg.fetchval("SELECT target_account_id FROM executions WHERE id=$1", execution)
        == account
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pg.execute("DELETE FROM platform_accounts WHERE id=$1", account)
    with pytest.raises(asyncpg.RaiseError, match="immutable"):
        await pg.execute(
            "UPDATE executions SET entity_ref='meta:account:another' WHERE id=$1", execution
        )
    other = await make_platform_account(pg, business)
    with pytest.raises(asyncpg.RaiseError, match="account_mismatch"):
        await pg.execute(
            """INSERT INTO execution_reservations
            (execution_id,business_id,platform_account_id,entity_ref,currency,positive_delta_minor,
             parameter,previous_value,proposed_value,diff_hash,created_at)
            VALUES($1,$2,$3,$4,'EUR',2000,'new_campaign:migration','null','{}',$5,now())""",
            execution,
            business,
            other,
            ref,
            "a" * 64,
        )
    with pytest.raises(DBAPIError, match="Cannot downgrade with account-target"):
        downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT
    assert (
        await pg.fetchval("SELECT target_account_id FROM executions WHERE id=$1", execution)
        == account
    )
