"""`decision_log` contra Postgres real (testcontainers): cadena de hash
correcta e inmutabilidad por trigger (data-model.md §DecisionLogEntry,
threat-model.md C-19). Los dobles no modelan CHECKs/UNIQUEs/triggers, por
eso este test corre contra un Postgres de verdad, no un mock."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_migrations(database_url: str) -> None:
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    os.environ["ADS_DATABASE_URL"] = database_url
    command.upgrade(config, "head")


def _to_asyncpg_dsn(sqlalchemy_dsn: str) -> str:
    return sqlalchemy_dsn.replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture(scope="module")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as container:
        async_dsn = container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql+asyncpg://"
        )
        _run_migrations(async_dsn)
        yield container


@pytest.fixture
async def pg_connection(
    postgres_container: PostgresContainer,
) -> AsyncIterator[asyncpg.Connection]:
    dsn = _to_asyncpg_dsn(postgres_container.get_connection_url())
    connection = await asyncpg.connect(dsn)
    try:
        yield connection
    finally:
        await connection.close()


async def _insert_entry(connection: asyncpg.Connection, event_type: str, payload: dict) -> int:
    row = await connection.fetchrow(
        """
        INSERT INTO decision_log (business_id, event_type, actor_kind, payload)
        VALUES (gen_random_uuid(), $1, 'system', $2::jsonb)
        RETURNING seq
        """,
        event_type,
        json.dumps(payload),
    )
    assert row is not None
    return int(row["seq"])


async def test_second_entry_chains_to_the_first(pg_connection: asyncpg.Connection) -> None:
    await _insert_entry(pg_connection, "test.first", {"a": 1})
    await _insert_entry(pg_connection, "test.second", {"a": 2})

    rows = await pg_connection.fetch(
        "SELECT seq, prev_hash, entry_hash FROM decision_log ORDER BY seq"
    )

    assert rows[0]["prev_hash"] == ""
    assert rows[1]["prev_hash"] == rows[0]["entry_hash"]
    assert rows[0]["entry_hash"] != rows[1]["entry_hash"]


async def test_entry_hash_matches_the_documented_formula(
    pg_connection: asyncpg.Connection,
) -> None:
    seq = await _insert_entry(pg_connection, "test.formula", {"x": "y"})

    row = await pg_connection.fetchrow(
        "SELECT prev_hash, entry_hash, payload FROM decision_log WHERE seq = $1", seq
    )
    assert row is not None

    canonical = f"{seq}|{row['prev_hash']}|{row['payload']}"
    expected_hash = hashlib.sha256(canonical.encode()).hexdigest()

    assert row["entry_hash"] == expected_hash


async def test_update_is_rejected_by_trigger(pg_connection: asyncpg.Connection) -> None:
    seq = await _insert_entry(pg_connection, "test.immutable_update", {})

    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await pg_connection.execute(
            "UPDATE decision_log SET event_type = 'tampered' WHERE seq = $1", seq
        )


async def test_delete_is_rejected_by_trigger(pg_connection: asyncpg.Connection) -> None:
    seq = await _insert_entry(pg_connection, "test.immutable_delete", {})

    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await pg_connection.execute("DELETE FROM decision_log WHERE seq = $1", seq)


async def test_truncate_is_rejected_by_trigger(pg_connection: asyncpg.Connection) -> None:
    await _insert_entry(pg_connection, "test.immutable_truncate", {})

    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await pg_connection.execute("TRUNCATE decision_log")
