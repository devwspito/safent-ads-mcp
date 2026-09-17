"""0005_catalog_crm: la atribucion no puede guardar dato personal (C-31)."""

from __future__ import annotations

import datetime as dt
import hashlib
import re

import asyncpg
import pytest

from tests.integration.migrations.conftest import (
    make_business,
    make_entity,
    make_platform_account,
)

pytestmark = pytest.mark.integration

# Vocabulario de dato personal en castellano e ingles. Si una columna se
# llama asi, el esquema ya perdio (threat-model.md C-31).
_PII_COLUMN_PATTERN = re.compile(
    r"(email|mail|phone|telefono|movil|nombre|apellido|\bname\b|dni|nif|nie|"
    r"passport|address|direccion|postal|birth|nacimiento|ip_address)",
    re.IGNORECASE,
)


async def test_no_pii_columns(pg: asyncpg.Connection) -> None:
    columns = [
        row["column_name"]
        for row in await pg.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'lead_attributions'
            """
        )
    ]

    assert columns, "lead_attributions no existe"
    offenders = [name for name in columns if _PII_COLUMN_PATTERN.search(name)]
    assert offenders == []
    assert "hashed_identity" in columns
    assert "identity_salt_ref" in columns


async def test_raw_identity_is_rejected(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="hashed_identity"):
        await pg.execute(
            """
            INSERT INTO lead_attributions (business_id, hashed_identity, identity_salt_ref,
                                           attribution_rung, conversion_kind, value_currency,
                                           occurred_at)
            VALUES ($1, 'cliente@example.com', 'salt/tenant/v3', 'aggregate', 'lead', 'EUR', now())
            """,
            business_id,
        )


async def test_attribution_upsert_does_not_duplicate(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)
    identity = hashlib.sha256(b"lead-1").hexdigest()
    occurred_at = dt.datetime(2026, 3, 14, 10, 30, tzinfo=dt.UTC)
    statement = """
        INSERT INTO lead_attributions (business_id, hashed_identity, identity_salt_ref,
                                       entity_ref, attribution_rung, conversion_kind,
                                       value_amount, value_currency, occurred_at)
        VALUES ($1, $2, 'salt/tenant/v3', $3, 'hashed_identity', 'lead', $4, 'EUR', $5)
        ON CONFLICT ON CONSTRAINT lead_attributions_natural_unique DO UPDATE
            SET value_amount = EXCLUDED.value_amount,
                observed_at  = now()
    """

    await pg.execute(statement, business_id, identity, entity["entity_ref"], 0, occurred_at)
    await pg.execute(statement, business_id, identity, entity["entity_ref"], 350, occurred_at)

    rows = await pg.fetch(
        "SELECT value_amount FROM lead_attributions WHERE hashed_identity = $1", identity
    )
    assert len(rows) == 1
    assert float(rows[0]["value_amount"]) == 350.0


async def test_click_rung_requires_a_click_hash(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)

    with pytest.raises(asyncpg.CheckViolationError, match="click_rung_check"):
        await pg.execute(
            """
            INSERT INTO lead_attributions (business_id, hashed_identity, identity_salt_ref,
                                           entity_ref, attribution_rung, conversion_kind,
                                           value_currency, occurred_at)
            VALUES ($1, $2, 'salt/tenant/v3', $3, 'click_id', 'lead', 'EUR', now())
            """,
            business_id,
            hashlib.sha256(b"lead-2").hexdigest(),
            entity["entity_ref"],
        )


async def test_calendar_event_window_must_be_ordered(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="calendar_events_window_check"):
        await pg.execute(
            """
            INSERT INTO calendar_events (business_id, name, region,
                                          window_start, window_end, source)
            VALUES ($1, 'Maestros Primaria', 'Madrid', '2027-03-01', '2027-02-01', 'panel')
            """,
            business_id,
        )


async def test_is_window_open_is_not_stored(pg: asyncpg.Connection) -> None:
    columns = [
        row["column_name"]
        for row in await pg.fetch(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'calendar_events'
            """
        )
    ]

    assert "is_window_open" not in columns
