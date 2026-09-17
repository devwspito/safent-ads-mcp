"""0031/0032/0033 (spec 027 T014): `customers`/`revenue_events`/
`crm_bridge_health` -- `upgrade` -> `downgrade` -> `upgrade` limpio (leer
la salida COMPLETA de alembic y verificar `alembic_version`: un
`lock_timeout` silencioso deja el codigo por delante del esquema, mismo
riesgo que documenta `feedback_migracion_fallida_en_silencio`).

Corre en una base AISLADA propia (no la `database_url` compartida de
sesion): `downgrade` a `base` en la base compartida tiraria las tablas que
el resto de la suite de integracion necesita durante la misma sesion de
pytest."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import asyncpg
import pytest
from testcontainers.community.postgres import PostgresContainer

from tests.conftest import alembic_head_revision, to_alembic_dsn, to_asyncpg_dsn, with_database
from tests.integration.migrations.conftest import downgrade, make_business, upgrade

pytestmark = pytest.mark.integration

_DATABASE_NAME = "ads_customers_revenue_migration"
_PREVIOUS_HEAD = "0030_sso_assertions_seen"
_REPO_ROOT = Path(__file__).resolve().parents[3]
# Mismo vocabulario que `test_catalog_crm.py::_PII_COLUMN_PATTERN`
# (threat-model.md C-31): si una columna se llama asi, el esquema ya perdio.
_PII_COLUMN_PATTERN = re.compile(
    r"(email|mail|phone|telefono|movil|nombre|apellido|\bname\b|dni|nif|nie|"
    r"passport|address|direccion|postal|birth|nacimiento|ip_address)",
    re.IGNORECASE,
)


@pytest.fixture
async def isolated_pg(postgres_container: PostgresContainer) -> asyncpg.Connection:
    base_url = postgres_container.get_connection_url()
    admin = await asyncpg.connect(to_asyncpg_dsn(base_url))
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{_DATABASE_NAME}"')
        await admin.execute(f'CREATE DATABASE "{_DATABASE_NAME}"')
    finally:
        await admin.close()
    url = with_database(to_alembic_dsn(base_url), _DATABASE_NAME)
    upgrade(url)
    connection = await asyncpg.connect(with_database(to_asyncpg_dsn(base_url), _DATABASE_NAME))
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def isolated_url(postgres_container: PostgresContainer) -> str:
    return with_database(to_alembic_dsn(postgres_container.get_connection_url()), _DATABASE_NAME)


async def test_upgrade_downgrade_upgrade_round_trips_cleanly(
    isolated_pg: asyncpg.Connection, isolated_url: str
) -> None:
    version_before = await isolated_pg.fetchval("SELECT version_num FROM alembic_version")
    assert version_before == alembic_head_revision()

    downgrade(isolated_url, _PREVIOUS_HEAD)
    tables_after_downgrade = await isolated_pg.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name IN "
        "('customers', 'identity_mappings', 'revenue_events', 'crm_bridge_health', "
        "'crm_bridge_tokens')"
    )
    assert tables_after_downgrade == []
    version_after_downgrade = await isolated_pg.fetchval("SELECT version_num FROM alembic_version")
    assert version_after_downgrade == _PREVIOUS_HEAD

    upgrade(isolated_url, "0033_crm_bridge_health")
    version_after_upgrade = await isolated_pg.fetchval("SELECT version_num FROM alembic_version")
    assert version_after_upgrade == "0033_crm_bridge_health"

    tables_after_upgrade = {
        row["table_name"]
        for row in await isolated_pg.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN "
            "('customers', 'identity_mappings', 'revenue_events', 'crm_bridge_health', "
            "'crm_bridge_tokens')"
        )
    }
    assert tables_after_upgrade == {
        "customers",
        "identity_mappings",
        "revenue_events",
        "crm_bridge_health",
        "crm_bridge_tokens",
    }


async def test_no_pii_columns_on_customers(isolated_pg: asyncpg.Connection) -> None:
    columns = [
        row["column_name"]
        for row in await isolated_pg.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'customers'"
        )
    ]
    offenders = [name for name in columns if _PII_COLUMN_PATTERN.search(name)]
    assert offenders == []
    assert "identity_digest" in columns


async def test_non_hex_identity_digest_is_rejected(isolated_pg: asyncpg.Connection) -> None:
    business_id = await make_business(isolated_pg)

    with pytest.raises(asyncpg.CheckViolationError):
        await isolated_pg.execute(
            """
            INSERT INTO customers (business_id, identity_digest, salt_version,
                                   attribution_rung, currency, first_seen_at, last_seen_at)
            VALUES ($1, 'cliente@example.com', 1, 'aggregate', 'EUR', now(), now())
            """,
            business_id,
        )


async def test_positive_refund_amount_is_rejected(isolated_pg: asyncpg.Connection) -> None:
    business_id = await make_business(isolated_pg)
    customer_id = await isolated_pg.fetchval(
        """
        INSERT INTO customers (business_id, identity_digest, salt_version,
                               attribution_rung, currency, first_seen_at, last_seen_at)
        VALUES ($1, $2, 1, 'aggregate', 'EUR', now(), now())
        RETURNING id
        """,
        business_id,
        _digest(),
    )

    with pytest.raises(asyncpg.CheckViolationError):
        await isolated_pg.execute(
            """
            INSERT INTO revenue_events (business_id, connector_id, customer_id, source_event_id,
                                        kind, amount_minor, currency, occurred_at, observed_at,
                                        mapping_version)
            VALUES ($1, 'connector-crm', $2, 'evt-1', 'refund', 500, 'EUR', now(), now(), 1)
            """,
            business_id,
            customer_id,
        )


async def test_reingesting_the_same_source_event_id_is_idempotent(
    isolated_pg: asyncpg.Connection,
) -> None:
    business_id = await make_business(isolated_pg)
    customer_id = await isolated_pg.fetchval(
        """
        INSERT INTO customers (business_id, identity_digest, salt_version,
                               attribution_rung, currency, first_seen_at, last_seen_at)
        VALUES ($1, $2, 1, 'aggregate', 'EUR', now(), now())
        RETURNING id
        """,
        business_id,
        _digest(),
    )
    statement = """
        INSERT INTO revenue_events (business_id, connector_id, customer_id, source_event_id,
                                    kind, amount_minor, currency, occurred_at, observed_at,
                                    mapping_version)
        VALUES ($1, 'connector-crm', $2, 'evt-dup', 'first_payment', 10000, 'EUR', now(), now(), 1)
        ON CONFLICT ON CONSTRAINT revenue_events_idempotency_unique DO NOTHING
        RETURNING id
    """

    first = await isolated_pg.fetchval(statement, business_id, customer_id)
    second = await isolated_pg.fetchval(statement, business_id, customer_id)

    assert first is not None
    assert second is None
    count = await isolated_pg.fetchval(
        "SELECT count(*) FROM revenue_events WHERE business_id = $1", business_id
    )
    assert count == 1


async def test_revenue_events_reject_update(isolated_pg: asyncpg.Connection) -> None:
    business_id = await make_business(isolated_pg)
    customer_id = await isolated_pg.fetchval(
        """
        INSERT INTO customers (business_id, identity_digest, salt_version,
                               attribution_rung, currency, first_seen_at, last_seen_at)
        VALUES ($1, $2, 1, 'aggregate', 'EUR', now(), now())
        RETURNING id
        """,
        business_id,
        _digest(),
    )
    event_id = await isolated_pg.fetchval(
        """
        INSERT INTO revenue_events (business_id, connector_id, customer_id, source_event_id,
                                    kind, amount_minor, currency, occurred_at, observed_at,
                                    mapping_version)
        VALUES ($1, 'connector-crm', $2, 'evt-immutable', 'first_payment', 10000, 'EUR', now(),
                now(), 1)
        RETURNING id
        """,
        business_id,
        customer_id,
    )

    with pytest.raises(asyncpg.RaiseError):
        await isolated_pg.execute(
            "UPDATE revenue_events SET amount_minor = 1 WHERE id = $1", event_id
        )


def _digest() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex
