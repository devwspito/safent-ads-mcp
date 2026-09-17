"""0053_federated_transaction_ip: `federated_login_transactions.ip_address`
(threat-model.md C-79, T084 hardening). Columna aditiva y nullable -- una
fila escrita antes de esta migracion no tiene IP conocida y sencillamente
no cuenta contra el tope de ninguna direccion."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from tests.integration.migrations.conftest import downgrade, upgrade
from tests.integration.migrations.test_connection_identity import (
    migration_sandbox as migration_sandbox,  # noqa: PLC0414 - pytest fixture re-export
)

pytestmark = pytest.mark.integration

PREVIOUS = "0052_federated_identity"
CURRENT = "0053_federated_transaction_ip"


def _fingerprint(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


async def _insert_transaction(pg, *, ip_address: str | None) -> str:
    state_hash = _fingerprint(uuid.uuid4().hex)
    await pg.execute(
        """
        INSERT INTO federated_login_transactions (state_hash, nonce_hash, purpose,
                                                  created_at, expires_at, ip_address)
        VALUES ($1, $2, 'login', now(), now() + interval '10 minutes', $3)
        """,
        state_hash,
        _fingerprint(f"nonce-{state_hash}"),
        ip_address,
    )
    return state_hash


async def test_roundtrip_on_empty_database(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    downgrade(dsn, PREVIOUS)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
    upgrade(dsn, CURRENT)
    assert await pg.fetchval("SELECT version_num FROM alembic_version") == CURRENT


async def test_the_column_exists_is_nullable_and_stores_a_plain_address(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)

    with_ip = await _insert_transaction(pg, ip_address="203.0.113.9")
    without_ip = await _insert_transaction(pg, ip_address=None)

    stored = await pg.fetchval(
        "SELECT host(ip_address) FROM federated_login_transactions WHERE state_hash = $1",
        with_ip,
    )
    stored_null = await pg.fetchval(
        "SELECT ip_address FROM federated_login_transactions WHERE state_hash = $1", without_ip
    )
    assert stored == "203.0.113.9"
    assert stored_null is None


async def test_downgrade_drops_the_column(migration_sandbox):
    dsn, pg = migration_sandbox
    upgrade(dsn, CURRENT)
    await _insert_transaction(pg, ip_address="203.0.113.9")

    downgrade(dsn, PREVIOUS)

    columns = {
        row["column_name"]
        for row in await pg.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'federated_login_transactions'"
        )
    }
    assert "ip_address" not in columns
