"""ADS-02 migration roundtrips preserve legacy references and partition FKs."""

from collections.abc import AsyncIterator
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy.exc import DBAPIError
from testcontainers.community.postgres import PostgresContainer

from tests.integration.migrations.conftest import (
    downgrade,
    make_approval,
    make_business,
    make_entity,
    make_platform_account,
    make_proposal,
    to_alembic_dsn,
    to_asyncpg_dsn,
    upgrade,
    with_database,
)

pytestmark = pytest.mark.integration
PREVIOUS = "0034_execution_reservations"
CURRENT = "0035_connection_identity"


@pytest.fixture
async def migration_sandbox(
    postgres_container: PostgresContainer,
) -> AsyncIterator[tuple[str, asyncpg.Connection]]:
    base = postgres_container.get_connection_url()
    name = f"ads_identity_{uuid4().hex}"
    admin = await asyncpg.connect(to_asyncpg_dsn(base))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    dsn = with_database(to_alembic_dsn(base), name)
    upgrade(dsn, PREVIOUS)
    pg = await asyncpg.connect(with_database(to_asyncpg_dsn(base), name))
    try:
        yield dsn, pg
    finally:
        await pg.close()
        admin = await asyncpg.connect(to_asyncpg_dsn(base))
        try:
            # No FORCE or session termination: a migration error must close its
            # own pooled connection, otherwise this cleanup fails visibly.
            await admin.execute(f'DROP DATABASE "{name}"')
        finally:
            await admin.close()


async def _constraint_graph(pg: asyncpg.Connection) -> list[tuple]:
    rows = await pg.fetch("""
        SELECT t.relname, c.conname, pg_get_constraintdef(c.oid),
               c.conislocal, c.coninhcount, parent.conname AS parent_name
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        LEFT JOIN pg_constraint parent ON parent.oid=c.conparentid
        WHERE n.nspname='public' AND c.confrelid='ad_entities'::regclass
        ORDER BY t.relname, c.conname
    """)
    return [tuple(row) for row in rows]


async def test_roundtrip_preserves_legacy_signed_history_and_partition_fk_graph(
    migration_sandbox: tuple[str, asyncpg.Connection],
) -> None:
    dsn, pg = migration_sandbox
    business = await make_business(pg)
    account = await make_platform_account(pg, business)
    entity = await make_entity(pg, business, account)
    proposal = await make_proposal(pg, business, entity["entity_ref"])
    approval = await make_approval(pg, proposal["id"], proposal["diff_hash"])

    async def snapshot() -> tuple:
        return (
            await pg.fetchval("SELECT entity_ref FROM ad_entities WHERE id=$1", entity["id"]),
            await pg.fetchval(
                "SELECT row_to_json(p)::text FROM proposals p WHERE id=$1", proposal["id"]
            ),
            await pg.fetchval("SELECT row_to_json(a)::text FROM approvals a WHERE id=$1", approval),
        )

    history_before = await snapshot()
    graph_before = await _constraint_graph(pg)
    assert any(row[-1] == "metrics_daily_entity_fk" for row in graph_before)
    upgrade(dsn, CURRENT)
    assert await snapshot() == history_before
    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    assert await snapshot() == history_before
    assert await _constraint_graph(pg) == graph_before
    upgrade(dsn, CURRENT)
    assert await snapshot() == history_before
    assert await _constraint_graph(pg) == graph_before


async def test_scoped_connection_blocks_downgrade_without_leaking_a_connection(
    migration_sandbox: tuple[str, asyncpg.Connection],
) -> None:
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    business = await make_business(pg)
    owner = await pg.fetchval("""INSERT INTO owners(email,password_hash)
        VALUES('migration@example.invalid','not-a-password') RETURNING id""")
    connection = uuid4()
    await pg.execute(
        """INSERT INTO platform_connections(id,business_id,owner_id,platform)
        VALUES($1,$2,$3,'google')""",
        connection,
        business,
        owner,
    )
    with pytest.raises(DBAPIError, match="Cannot downgrade with scoped connections"):
        downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT
    assert await pg.fetchval("SELECT id FROM platform_connections") == connection
    # The fixture's unforced DROP DATABASE also verifies cleanup after failure.
