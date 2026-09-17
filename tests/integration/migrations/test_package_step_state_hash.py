"""0049 -- `campaign_package_steps.confirmed_state_hash` (T123/AL-4): el
recibo confirmado de un paso, archivado para que su HIJO firme su propio
`expected_state_hash` (data-model.md R2.8, generalizado de la activacion a
todo paso con padre). Columna aditiva; el CHECK solo admite un valor junto
a `state = 'done'` y `kind <> 'upload_creative'` (esa subida no pasa por el
chokepoint y nunca tiene un `ExecutionAttempt` que confirme nada)."""

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError

from tests.integration.migrations.conftest import downgrade, upgrade
from tests.integration.migrations.test_campaign_packages import insert_package, seed_scope
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.migrations.test_package_approval_envelope import insert_publication

pytestmark = pytest.mark.integration
PREVIOUS = "0048_step_parent_check_online"
CURRENT = "0049_package_step_state_hash"


async def seed_publication(pg) -> str:
    business, account_ref, offering, _account = await seed_scope(pg)
    package = await insert_package(pg, business, account_ref, offering)
    authorization = await pg.fetchval(
        """INSERT INTO approvals (proposal_id, kind, decision, diff_hash,
                                  guardrail_verdict_hash, issued_by, channel, signature,
                                  expires_at, subject_kind, subject_id)
           VALUES (NULL, 'human_approval', 'approved', $1, $2, 'owner', 'panel',
                   'firma-ed25519-de-prueba', now() + interval '30 minutes',
                   'package', $3)
           RETURNING id""",
        "1" * 64,
        "2" * 64,
        package,
    )
    return await insert_publication(pg, package, authorization)


async def insert_step(pg, publication_id, *, step_index, kind, local_ref, parent_local_ref=None):
    await pg.execute(
        "INSERT INTO campaign_package_steps (publication_id, step_index, kind, local_ref, "
        "parent_local_ref) VALUES ($1, $2, $3, $4, $5)",
        publication_id,
        step_index,
        kind,
        local_ref,
        parent_local_ref,
    )


async def advance_to_done(pg, publication_id, step_index, *, confirmed_state_hash):
    await pg.execute(
        "UPDATE campaign_package_steps SET state = 'running' "
        "WHERE publication_id = $1 AND step_index = $2",
        publication_id,
        step_index,
    )
    await pg.execute(
        "UPDATE campaign_package_steps SET state = 'done', confirmed_state_hash = $3 "
        "WHERE publication_id = $1 AND step_index = $2",
        publication_id,
        step_index,
        confirmed_state_hash,
    )


async def test_the_column_exists_and_defaults_to_null(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    publication_id = await seed_publication(pg)
    await insert_step(
        pg, publication_id, step_index=0, kind="create_campaign", local_ref="campaign"
    )

    value = await pg.fetchval(
        "SELECT confirmed_state_hash FROM campaign_package_steps "
        "WHERE publication_id = $1 AND step_index = 0",
        publication_id,
    )

    assert value is None


async def test_confirmed_state_hash_survives_pending_running_done(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    publication_id = await seed_publication(pg)
    await insert_step(
        pg, publication_id, step_index=0, kind="create_campaign", local_ref="campaign"
    )

    await advance_to_done(pg, publication_id, 0, confirmed_state_hash="h" * 64)

    row = await pg.fetchrow(
        "SELECT state, confirmed_state_hash FROM campaign_package_steps "
        "WHERE publication_id = $1 AND step_index = 0",
        publication_id,
    )
    assert row["state"] == "done"
    assert row["confirmed_state_hash"] == "h" * 64


async def test_confirmed_state_hash_on_a_step_still_pending_is_rejected(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    publication_id = await seed_publication(pg)
    await insert_step(
        pg, publication_id, step_index=0, kind="create_campaign", local_ref="campaign"
    )

    with pytest.raises(
        asyncpg.CheckViolationError, match="campaign_package_steps_confirmed_state_hash_check"
    ):
        await pg.execute(
            "UPDATE campaign_package_steps SET confirmed_state_hash = $3 "
            "WHERE publication_id = $1 AND step_index = $2",
            publication_id,
            0,
            "h" * 64,
        )


async def test_confirmed_state_hash_on_upload_creative_is_rejected_even_when_done(
    migration_sandbox,
):
    """`upload_creative` nunca pasa por el chokepoint (BL-6): no tiene
    `ExecutionAttempt` ni `confirmed_state_hash` que archivar."""
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    publication_id = await seed_publication(pg)
    await insert_step(
        pg, publication_id, step_index=0, kind="upload_creative", local_ref="img#abcdef123456"
    )
    await pg.execute(
        "UPDATE campaign_package_steps SET state = 'running' "
        "WHERE publication_id = $1 AND step_index = 0",
        publication_id,
    )

    with pytest.raises(
        asyncpg.CheckViolationError, match="campaign_package_steps_confirmed_state_hash_check"
    ):
        await pg.execute(
            "UPDATE campaign_package_steps SET state = 'done', confirmed_state_hash = $2 "
            "WHERE publication_id = $1 AND step_index = 0",
            publication_id,
            "h" * 64,
        )


async def test_downgrade_refuses_to_erase_confirmed_state_hash_history(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    publication_id = await seed_publication(pg)
    await insert_step(
        pg, publication_id, step_index=0, kind="create_campaign", local_ref="campaign"
    )
    await advance_to_done(pg, publication_id, 0, confirmed_state_hash="h" * 64)

    with pytest.raises(DBAPIError, match="campaign_package_steps_confirmed_state_hash_present"):
        downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT


async def test_roundtrip_on_empty_database(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    upgrade(dsn, CURRENT)
    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    upgrade(dsn, CURRENT)
