"""0039 never rewrites legacy signed bytes or drops managed history.

Estos tests congelan el esquema en 0039 a proposito, asi que las filas entran
por `insert_managed_proposal`/`make_proposal` (SQL crudo) y no por
`SqlProposalRepository`: el repositorio habla el esquema de cabecera
(`proposed_by`, 0043) y contra una revision congelada no compila. Lo que se
prueba aqui es el trigger de contexto gestionado y el guardian de su
downgrade, no el repositorio -- de ese se ocupa `tests/integration/proposals`,
que corre contra cabecera."""

import asyncio
import json

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.integration.migrations.conftest import (
    downgrade,
    insert_managed_proposal,
    make_business,
    make_entity,
    make_platform_account,
    make_proposal,
    upgrade,
)
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - fixture re-export
)
from tests.integration.proposals.test_managed_signed_context import scoped_pair

pytestmark = pytest.mark.integration
PREVIOUS = "0038_owner_action_confirmations"
CURRENT = "0039_managed_signed_context"


async def test_empty_additive_migration_roundtrip(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    before = await pg.fetchval("SELECT count(*) FROM proposals")
    upgrade(dsn, CURRENT)
    assert await pg.fetchval("SELECT count(*) FROM proposals") == before
    for table in ("proposals", "approvals", "execution_reservations"):
        assert (
            await pg.fetchval(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name=$1 AND column_name='managed_binding'",
                table,
            )
            == 1
        )
    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    upgrade(dsn, CURRENT)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT


async def test_managed_history_blocks_downgrade_without_altering_data(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    engine = create_async_engine(dsn)
    try:
        async with AsyncSession(engine) as session:
            business, a, _, binding, _ = await scoped_pair(session)
            await insert_managed_proposal(session, business, a, binding)
            await session.commit()
        before = await pg.fetchval("SELECT row_to_json(p)::text FROM proposals p")
        with pytest.raises(DBAPIError, match="managed_context_history_requires_current_schema"):
            downgrade(dsn, PREVIOUS)
        assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT
        assert await pg.fetchval("SELECT row_to_json(p)::text FROM proposals p") == before
    finally:
        await engine.dispose()


async def test_legacy_hash_payload_and_timestamps_are_not_backfilled(migration_sandbox):
    """La fila legada se escribe ANTES de 0039 -- legada de verdad, no una fila
    nueva a la que se le quita la columna despues."""
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    business = await make_business(pg)
    entity = await make_entity(pg, business, await make_platform_account(pg, business))
    await make_proposal(pg, business, entity["entity_ref"])
    snapshot = "SELECT row_to_json(p)::text FROM proposals p"
    old = json.loads(await pg.fetchval(snapshot))

    upgrade(dsn, CURRENT)
    new = json.loads(await pg.fetchval(snapshot))
    assert new.pop("managed_binding") is None
    assert new == old
    # Sin historia gestionada la vuelta atras es legal, y tampoco reescribe.
    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    assert json.loads(await pg.fetchval(snapshot)) == old


async def test_downgrade_waits_before_check_and_preserves_concurrent_managed_insert(
    migration_sandbox,
):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    engine = create_async_engine(dsn)
    pending = None
    try:
        async with AsyncSession(engine) as session:
            business, a, _, binding, _ = await scoped_pair(session)
            await session.commit()
            proposal = await insert_managed_proposal(session, business, a, binding)
            # Uncommitted INSERT holds RowExclusive. A check performed before
            # the DDL lock cannot see it; dropping the column afterward loses it.
            pending = asyncio.create_task(asyncio.to_thread(downgrade, dsn, PREVIOUS))
            async with asyncio.timeout(8):
                while not await pg.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE relation='proposals'::regclass AND NOT granted)"
                ):
                    await asyncio.sleep(0.02)
            await session.commit()
        with pytest.raises(DBAPIError, match="managed_context_history_requires_current_schema"):
            await asyncio.wait_for(pending, 10)
        assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT
        assert await pg.fetchval(
            "SELECT managed_binding->>'grant_id' FROM proposals WHERE id=$1", proposal
        ) == str(binding.grant_id)
    finally:
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await engine.dispose()
