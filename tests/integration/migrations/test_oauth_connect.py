"""0013_oauth_connect: `oauth_connect_sessions` (un solo uso, `state_hash`
unico) y las columnas de salud nuevas de `credential_refs`."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from tests.integration.migrations.conftest import make_business

pytestmark = pytest.mark.integration

_INSERT_SESSION = """
    INSERT INTO oauth_connect_sessions (business_id, owner_id, provider, state_hash,
                                        expires_at)
    VALUES ($1, $2, 'google', $3, now() + interval '10 minutes')
    RETURNING id
"""


async def _owner_id(pg: asyncpg.Connection) -> uuid.UUID:
    return await pg.fetchval(
        """
        INSERT INTO owners (email, password_hash)
        VALUES ($1, 'argon2id$fake-hash-for-tests')
        RETURNING id
        """,
        f"owner-{uuid.uuid4().hex[:12]}@example.com",
    )


async def test_state_hash_is_unique(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    owner_id = await _owner_id(pg)
    state_hash = uuid.uuid4().hex

    await pg.fetchval(_INSERT_SESSION, business_id, owner_id, state_hash)

    with pytest.raises(asyncpg.UniqueViolationError):
        await pg.fetchval(_INSERT_SESSION, business_id, owner_id, state_hash)


async def test_completed_session_requires_completed_at(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    owner_id = await _owner_id(pg)

    with pytest.raises(asyncpg.CheckViolationError):
        await pg.execute(
            """
            INSERT INTO oauth_connect_sessions (business_id, owner_id, provider, state_hash,
                                                status, expires_at)
            VALUES ($1, $2, 'google', $3, 'ok', now() + interval '10 minutes')
            """,
            business_id,
            owner_id,
            uuid.uuid4().hex,
        )


async def test_resolving_a_session_sets_completed_at(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    owner_id = await _owner_id(pg)
    session_id = await pg.fetchval(
        _INSERT_SESSION, business_id, owner_id, uuid.uuid4().hex
    )

    resolved = await pg.fetchrow(
        """
        UPDATE oauth_connect_sessions
           SET status = 'ok', completed_at = now()
         WHERE id = $1
        RETURNING status, completed_at
        """,
        session_id,
    )

    assert resolved["status"] == "ok"
    assert resolved["completed_at"] is not None


async def test_unknown_provider_is_rejected(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    owner_id = await _owner_id(pg)

    with pytest.raises(asyncpg.CheckViolationError):
        await pg.execute(
            """
            INSERT INTO oauth_connect_sessions (business_id, owner_id, provider, state_hash,
                                                expires_at)
            VALUES ($1, $2, 'tiktok', $3, now() + interval '10 minutes')
            """,
            business_id,
            owner_id,
            uuid.uuid4().hex,
        )


async def test_credential_refs_default_to_connected(pg: asyncpg.Connection) -> None:
    row = await pg.fetchrow(
        """
        INSERT INTO credential_refs (platform, alias)
        VALUES ('google', $1)
        RETURNING status, obtained_at, last_validated_at, revoked_at
        """,
        f"alias-{uuid.uuid4().hex[:12]}",
    )

    assert row["status"] == "CONNECTED"
    assert row["obtained_at"] is None
    assert row["last_validated_at"] is None
    assert row["revoked_at"] is None


async def test_credential_refs_rejects_unknown_status(pg: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await pg.execute(
            """
            INSERT INTO credential_refs (platform, alias, status)
            VALUES ('meta', $1, 'BROKEN')
            """,
            f"alias-{uuid.uuid4().hex[:12]}",
        )
