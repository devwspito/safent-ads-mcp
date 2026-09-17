"""0043 anade `proposals.proposed_by`: aditivo, sin backfill, sin tocar
`diff_hash` ni la firma de `approvals` (004 tasks.md A8). La bajada nunca
borra procedencia en silencio -- mismo guardian que 0041/0042
(`campaign_drafts_not_empty`, `campaign_packages_not_empty`): con una fila
que ya sabe quien la propuso, se para y lo dice."""

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
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - pytest fixture re-export
)

pytestmark = pytest.mark.integration
PREVIOUS = "0042_campaign_packages"
CURRENT = "0043_proposed_by"


async def _proposals_columns(pg):
    rows = await pg.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='proposals'"
    )
    return {row["column_name"] for row in rows}


async def _seed_proposal(pg):
    business = await make_business(pg)
    entity = await make_entity(pg, business, await make_platform_account(pg, business))
    proposal = await make_proposal(pg, business, entity["entity_ref"])
    await make_approval(pg, proposal["id"], proposal["diff_hash"])
    return proposal


async def test_0043_es_aditivo_y_la_bajada_no_toca_ninguna_otra_columna(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    proposal = await _seed_proposal(pg)
    columns_before = await _proposals_columns(pg)
    approvals_before = await pg.fetchval("SELECT row_to_json(a)::text FROM approvals a")

    upgrade(dsn, CURRENT)
    assert await _proposals_columns(pg) == columns_before | {"proposed_by"}
    assert (
        await pg.fetchval("SELECT proposed_by FROM proposals WHERE id=$1", proposal["id"]) is None
    )
    assert (
        await pg.fetchval("SELECT diff_hash FROM proposals WHERE id=$1", proposal["id"])
        == proposal["diff_hash"]
    )
    assert await pg.fetchval("SELECT row_to_json(a)::text FROM approvals a") == approvals_before
    indexdef = await pg.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname='ix_proposals_proposed_by'"
    )
    assert "proposed_by IS NOT NULL" in indexdef

    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    assert await _proposals_columns(pg) == columns_before
    assert (
        await pg.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE indexname='ix_proposals_proposed_by'"
        )
        == 0
    )

    upgrade(dsn, CURRENT)
    assert (
        await pg.fetchval("SELECT proposed_by FROM proposals WHERE id=$1", proposal["id"]) is None
    )
    assert (
        await pg.fetchval("SELECT diff_hash FROM proposals WHERE id=$1", proposal["id"])
        == proposal["diff_hash"]
    )


async def test_downgrade_refuses_to_silently_drop_a_known_proposer(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    proposal = await _seed_proposal(pg)
    await pg.execute(
        "UPDATE proposals SET proposed_by=$1 WHERE id=$2", "person:user-1", proposal["id"]
    )

    with pytest.raises(DBAPIError, match="proposals_proposed_by_not_empty"):
        downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT
    assert (
        await pg.fetchval("SELECT proposed_by FROM proposals WHERE id=$1", proposal["id"])
        == "person:user-1"
    )
