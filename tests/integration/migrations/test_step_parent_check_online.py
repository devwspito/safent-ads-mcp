"""M4 (revision de codigo): `0048_step_parent_check_online` reescribe
`campaign_package_steps_parent_check` con `NOT VALID` + `VALIDATE
CONSTRAINT` en vez del `ADD CONSTRAINT` liso de `0047_activate_step_parent`
-- la razon de ser es que valide filas EXISTENTES sin errores y sin
perder el vocabulario correcto (`activate_campaign` exige padre)."""

import asyncpg
import pytest
from ulid import ULID

from tests.integration.migrations.conftest import downgrade, upgrade
from tests.integration.migrations.test_campaign_packages import insert_package, seed_scope
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.migrations.test_package_approval_envelope import insert_publication

pytestmark = pytest.mark.integration
PREVIOUS = "0047_activate_step_parent"
CURRENT = "0048_step_parent_check_online"


async def constraint_def(pg):
    return await pg.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'campaign_package_steps_parent_check' "
        "AND conrelid = 'campaign_package_steps'::regclass"
    )


async def constraint_is_validated(pg):
    return await pg.fetchval(
        "SELECT convalidated FROM pg_constraint "
        "WHERE conname = 'campaign_package_steps_parent_check' "
        "AND conrelid = 'campaign_package_steps'::regclass"
    )


async def insert_full_step_plan(pg, publication_id):
    """Los cuatro pasos con la forma que exige `0047`/`0048`: solo
    `create_ad_set`/`create_ad`/`activate_campaign` llevan padre."""
    await pg.executemany(
        "INSERT INTO campaign_package_steps (publication_id, step_index, kind, local_ref, "
        "parent_local_ref) VALUES ($1, $2, $3, $4, $5)",
        [
            (publication_id, 0, "create_campaign", "campaign", None),
            (publication_id, 1, "create_ad_set", "as#1", "campaign"),
            (publication_id, 2, "create_ad", "as#1/ad#1", "as#1"),
            (publication_id, 3, "activate_campaign", "campaign", "campaign"),
        ],
    )


async def seed_publication_with_steps(pg):
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
        "e" * 64,
        "f" * 64,
        package,
    )
    publication = await insert_publication(pg, package, authorization)
    await insert_full_step_plan(pg, publication)
    return publication


async def test_the_check_survives_the_online_rewrite_and_stays_validated(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    before = await constraint_def(pg)

    upgrade(dsn, CURRENT)

    assert await constraint_def(pg) == before
    assert await constraint_is_validated(pg) is True


async def test_existing_valid_rows_survive_the_rewrite_without_error(migration_sandbox):
    """El punto de `NOT VALID` + `VALIDATE`: filas YA presentes (creadas
    bajo `0047`) siguen siendo aceptadas -- `VALIDATE CONSTRAINT` las
    escanea, nunca las rechaza si ya cumplian la regla."""
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    publication = await seed_publication_with_steps(pg)

    upgrade(dsn, CURRENT)

    rows = await pg.fetch(
        "SELECT kind, parent_local_ref FROM campaign_package_steps "
        "WHERE publication_id = $1 ORDER BY step_index",
        publication,
    )
    assert [dict(row) for row in rows] == [
        {"kind": "create_campaign", "parent_local_ref": None},
        {"kind": "create_ad_set", "parent_local_ref": "campaign"},
        {"kind": "create_ad", "parent_local_ref": "as#1"},
        {"kind": "activate_campaign", "parent_local_ref": "campaign"},
    ]


async def test_activate_campaign_without_a_parent_is_still_rejected(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    publication = str(ULID())
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
        "a" * 64,
        "b" * 64,
        package,
    )
    publication = await insert_publication(pg, package, authorization)

    with pytest.raises(asyncpg.CheckViolationError, match="campaign_package_steps_parent_check"):
        await pg.execute(
            "INSERT INTO campaign_package_steps (publication_id, step_index, kind, "
            "local_ref, parent_local_ref) VALUES ($1, 0, 'activate_campaign', 'campaign', NULL)",
            publication,
        )


async def test_downgrade_restores_the_state_left_by_0047(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    at_current = await constraint_def(pg)

    downgrade(dsn, PREVIOUS)

    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    assert await constraint_def(pg) == at_current

    upgrade(dsn, CURRENT)
