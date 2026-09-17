"""Additive consumed-intent metadata; never erase a still-live replay barrier."""

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from tests.integration.migrations.conftest import downgrade, upgrade
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - pytest fixture re-export
)

pytestmark = pytest.mark.integration


async def test_upgrade_and_safe_downgrade_preserve_owner_and_session(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, "0037_execution_account_targets")
    owner, session, nonce = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await pg.execute(
        "INSERT INTO owners(id,email,password_hash) VALUES($1,$2,'fixture')",
        owner,
        f"{owner}@example.test",
    )
    await pg.execute(
        "INSERT INTO sessions(id,owner_id,token_hash,created_at,expires_at) "
        "VALUES($1,$2,$3,now(),now()+interval '1 hour')",
        session,
        owner,
        "a" * 64,
    )
    before = await pg.fetchval("SELECT row_to_json(s)::text FROM sessions s WHERE id=$1", session)
    upgrade(dsn, "0038_owner_action_confirmations")
    await pg.execute(
        "INSERT INTO owner_action_confirmations(nonce,session_id,owner_id,binding_hash,expires_at) "
        "VALUES($1,$2,$3,$4,now()+interval '2 minutes')",
        nonce,
        session,
        owner,
        "b" * 64,
    )
    with pytest.raises(DBAPIError, match="Cannot downgrade"):
        downgrade(dsn, "0037_execution_account_targets")
    assert await pg.fetchval("SELECT count(*) FROM owner_action_confirmations") == 1
    await pg.execute("UPDATE owner_action_confirmations SET expires_at=now()-interval '1 second'")
    downgrade(dsn, "0037_execution_account_targets")
    assert (
        await pg.fetchval("SELECT row_to_json(s)::text FROM sessions s WHERE id=$1", session)
        == before
    )
    upgrade(dsn, "0038_owner_action_confirmations")
