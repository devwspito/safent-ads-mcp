"""0040 maps Meta claims explicitly and never rewrites approved history.

Mismo criterio que `test_managed_signed_context`: el esquema se congela en
0039/0040, asi que las propuestas entran por `insert_managed_proposal` (SQL
crudo con las claims firmadas del dominio) y no por `SqlProposalRepository`,
que escribe columnas de cabecera (`proposed_by`, 0043). Lo que se prueba es
`ads_managed_provider_account` y el guardian de su downgrade."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef
from safent_ads.shared.managed_ads import enterprise_account_ref
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.integration.execution.test_physical_controls_sql import clone_connection
from tests.integration.migrations.conftest import downgrade, insert_managed_proposal, upgrade
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.proposals.test_managed_signed_context import managed_command, scoped_pair
from tests.unit.proposals.test_managed_binding import binding

pytestmark = pytest.mark.integration
CURRENT = "0040_managed_provider_account"
PREVIOUS = "0039_managed_signed_context"


async def meta_pair(session):
    original = campaign_ref(str(uuid4().int)[:18], "meta")
    business = BusinessId(await seed_entity(session, original))
    a, raw_a = await clone_connection(session, original)
    b, raw_b = await clone_connection(session, original)
    return (
        business,
        a,
        b,
        replace(binding(), account=enterprise_account_ref(AccountRef.parse(raw_a))),
        replace(binding(), account=enterprise_account_ref(AccountRef.parse(raw_b))),
    )


async def test_empty_roundtrip_and_exact_mapping_table(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    for platform, raw, expected in [
        ("google", "00123", "00123"),
        ("meta", "00123", "act_00123"),
        ("meta", "act_123", None),
        ("meta", "１２３", None),
        ("google", "123/1", None),
        ("other", "123", None),
    ]:
        assert (
            await pg.fetchval("SELECT ads_managed_provider_account($1,$2)", platform, raw)
            == expected
        )
    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    upgrade(dsn, CURRENT)


async def test_meta_campaign_and_account_targets_keep_numeric_claims_and_exact_connection(
    migration_sandbox,
):
    dsn, pg = migration_sandbox
    upgrade(dsn, PREVIOUS)
    engine = create_async_engine(dsn)
    try:
        async with AsyncSession(engine) as session:
            business, a, b, bind_a, bind_b = await meta_pair(session)
            await session.commit()
            # Reproduce the existing incompatibility against the actual 0039
            # trigger, using exactly the same signed claims before/after upgrade.
            with pytest.raises(DBAPIError, match="managed_binding_account_mismatch"):
                async with session.begin_nested():
                    await insert_managed_proposal(session, business, a, bind_a)
            await session.commit()
            upgrade(dsn, CURRENT)
            wrong_account = replace(
                bind_a, account=replace(bind_a.account, external_account_id="999")
            )
            with pytest.raises(DBAPIError, match="managed_binding_account_mismatch"):
                async with session.begin_nested():
                    await insert_managed_proposal(session, business, a, wrong_account)
            first = await insert_managed_proposal(session, business, a, bind_a)
            # Connection swap cannot authorize even the same physical account.
            with pytest.raises(ValueError):
                managed_command(business, a, bind_b)
            account = bind_b.provider_account
            account_ref = EntityRef(
                account.platform,
                EntityLevel.ACCOUNT,
                account.external_account_id,
                account.business_id,
                account.connection_id,
            )
            second = await insert_managed_proposal(session, business, account_ref, bind_b)
            await session.commit()
        for proposal in (first, second):
            assert (
                await pg.fetchval(
                    "SELECT managed_binding->>'external_account_id' FROM proposals WHERE id=$1",
                    proposal,
                )
                == bind_a.account.external_account_id
            )
        before = await pg.fetchval(
            "SELECT jsonb_agg(to_jsonb(p) ORDER BY id)::text FROM proposals p"
        )
        with pytest.raises(DBAPIError, match="managed_meta_history_requires_provider_mapping"):
            downgrade(dsn, PREVIOUS)
        assert (
            await pg.fetchval("SELECT jsonb_agg(to_jsonb(p) ORDER BY id)::text FROM proposals p")
            == before
        )
    finally:
        await engine.dispose()


async def test_google_signed_history_survives_mapping_downgrade(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    engine = create_async_engine(dsn)
    try:
        async with AsyncSession(engine) as session:
            business, ref, _, bound, _ = await scoped_pair(session)
            await insert_managed_proposal(session, business, ref, bound)
            await session.commit()
        before = await pg.fetchval("SELECT row_to_json(p)::text FROM proposals p")
        downgrade(dsn, PREVIOUS)
        assert await pg.fetchval("SELECT row_to_json(p)::text FROM proposals p") == before
        upgrade(dsn, CURRENT)
        assert await pg.fetchval("SELECT row_to_json(p)::text FROM proposals p") == before
    finally:
        await engine.dispose()


async def test_downgrade_locks_before_checking_concurrent_meta_insert(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    engine = create_async_engine(dsn)
    pending = None
    try:
        async with AsyncSession(engine) as session:
            business, ref, _, bound, _ = await meta_pair(session)
            await session.commit()
            proposal = await insert_managed_proposal(session, business, ref, bound)
            pending = asyncio.create_task(asyncio.to_thread(downgrade, dsn, PREVIOUS))
            async with asyncio.timeout(8):
                while not await pg.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                    "WHERE relation='proposals'::regclass AND NOT granted)"
                ):
                    await asyncio.sleep(0.02)
            await session.commit()
        with pytest.raises(DBAPIError, match="managed_meta_history_requires_provider_mapping"):
            await asyncio.wait_for(pending, 10)
        assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT
        assert (
            await pg.fetchval(
                "SELECT managed_binding->>'external_account_id' FROM proposals WHERE id=$1",
                proposal,
            )
            == bound.account.external_account_id
        )
    finally:
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await engine.dispose()
