"""0046 -- `executions` admite un intento de paso de paquete (BL-4, AL-3):
la clave de idempotencia `pkg-<publicacion>-<indice>`, el enlace a su
publicacion (para que el ciclo generico nunca la reclame) y el recurso
creado (perdido hasta ahora en la frontera de `execution`)."""

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError

from tests.integration.migrations.conftest import (
    downgrade,
    make_approval,
    make_entity,
    make_proposal,
    upgrade,
)
from tests.integration.migrations.test_campaign_packages import insert_package, seed_scope
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.migrations.test_package_approval_envelope import insert_publication

pytestmark = pytest.mark.integration
PREVIOUS = "0045_package_step_auth_chain"
CURRENT = "0046_execution_package_steps"


async def seed_execution_context(pg):
    business, account_ref, offering, account = await seed_scope(pg)
    package = await insert_package(pg, business, account_ref, offering)
    entity = await make_entity(pg, business, account)
    proposal = await make_proposal(pg, business, entity["entity_ref"])
    authorization = await make_approval(pg, proposal["id"], proposal["diff_hash"])
    publication = await insert_publication(pg, package, authorization)
    return business, entity, proposal, authorization, publication


async def insert_execution(
    pg,
    *,
    business,
    entity_ref,
    proposal_id,
    authorization_id,
    idempotency_key,
    package_publication_id=None,
    created_external_id=None,
):
    return await pg.fetchval(
        """INSERT INTO executions (proposal_id, authorization_id, business_id, entity_ref,
               idempotency_key, previous_value, platform_state_hash_before,
               package_publication_id, created_external_id)
           VALUES ($1, $2, $3, $4, $5, '{"amount": "1.00", "currency": "EUR"}'::jsonb,
                   $6, $7, $8)
           RETURNING id""",
        proposal_id,
        authorization_id,
        business,
        entity_ref,
        idempotency_key,
        "a" * 64,
        package_publication_id,
        created_external_id,
    )


async def test_package_idempotency_key_shape_is_admitted_and_exec_shape_unchanged(
    migration_sandbox,
):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, entity, proposal, authorization, publication = await seed_execution_context(pg)

    execution_id = await insert_execution(
        pg,
        business=business,
        entity_ref=entity["entity_ref"],
        proposal_id=proposal["id"],
        authorization_id=authorization,
        idempotency_key=f"pkg-{publication}-00",
        package_publication_id=publication,
        created_external_id="meta:campaign:123",
    )
    assert execution_id is not None

    with pytest.raises(asyncpg.CheckViolationError, match="executions_idempotency_key_check"):
        await insert_execution(
            pg,
            business=business,
            entity_ref=entity["entity_ref"],
            proposal_id=proposal["id"],
            authorization_id=authorization,
            idempotency_key="pkg-not-a-ulid-00",
        )


async def test_generic_claim_index_excludes_package_steps(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, entity, proposal, authorization, publication = await seed_execution_context(pg)
    await insert_execution(
        pg,
        business=business,
        entity_ref=entity["entity_ref"],
        proposal_id=proposal["id"],
        authorization_id=authorization,
        idempotency_key=f"pkg-{publication}-00",
        package_publication_id=publication,
    )

    # El indice parcial de reclamo generico (AL-3) no incluye esta fila:
    # `SqlExecutionQueue.claim_next()` sin `proposal_id` nunca la vera.
    claimable = await pg.fetchval(
        "SELECT count(*) FROM executions WHERE package_publication_id IS NULL "
        "AND outcome = 'CLAIMED'"
    )
    assert claimable == 0


async def test_downgrade_refuses_to_erase_package_step_executions(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business, entity, proposal, authorization, publication = await seed_execution_context(pg)
    await insert_execution(
        pg,
        business=business,
        entity_ref=entity["entity_ref"],
        proposal_id=proposal["id"],
        authorization_id=authorization,
        idempotency_key=f"pkg-{publication}-00",
        package_publication_id=publication,
    )

    with pytest.raises(DBAPIError, match="executions_package_step_history_present"):
        downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT


async def test_roundtrip_on_empty_database(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    upgrade(dsn, CURRENT)
    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    upgrade(dsn, CURRENT)
