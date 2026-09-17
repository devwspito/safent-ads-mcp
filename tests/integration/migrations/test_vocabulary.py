"""`0025_vocabulary` (vocabulary.md T175-T176): sobre una base recien
migrada a `0023_owner_settings`, siembra filas con el vocabulario viejo
(`courses`/`convocatorias`/`conversion_kind='enrolment'`), sube a head y
comprueba los renombres de tabla/columna/constraint/indice y la reescritura
de datos; despues baja y comprueba la simetria completa -- mismo patron
que `test_migration_round_trip.py::test_upgrade_downgrade_upgrade`, pero
centrado en esta revision."""

from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from testcontainers.community.postgres import PostgresContainer

from tests.integration.migrations.conftest import (
    downgrade,
    to_alembic_dsn,
    to_asyncpg_dsn,
    upgrade,
    with_database,
)

pytestmark = pytest.mark.integration

_VOCAB_DB = "ads_vocabulary_migration"


async def _seed_legacy_rows(dsn: str) -> dict[str, uuid.UUID]:
    connection = await asyncpg.connect(dsn)
    try:
        business_id = await connection.fetchval(
            """
            INSERT INTO businesses (slug, name, timezone, reference_currency)
            VALUES ($1, 'Negocio de prueba', 'Europe/Madrid', 'EUR') RETURNING id
            """,
            f"vocab-{uuid.uuid4().hex[:10]}",
        )
        course_id = await connection.fetchval(
            """
            INSERT INTO courses (business_id, code, title, cuerpo, especialidad)
            VALUES ($1, $2, 'Curso de prueba', 'cuerpo-x', 'especialidad-y') RETURNING id
            """,
            business_id,
            f"c-{uuid.uuid4().hex[:8]}",
        )
        convocatoria_id = await connection.fetchval(
            """
            INSERT INTO convocatorias (business_id, course_id, denominacion, comunidad,
                                       fecha_inicio_plazo, fecha_fin_plazo, fuente)
            VALUES ($1, $2, 'Conv 2026', 'Madrid', '2026-01-01', '2026-02-01', 'oficial')
            RETURNING id
            """,
            business_id,
            course_id,
        )
        lead_attribution_id = await connection.fetchval(
            """
            INSERT INTO lead_attributions (business_id, hashed_identity, identity_salt_ref,
                                           convocatoria_id, attribution_rung, conversion_kind,
                                           value_currency, occurred_at)
            VALUES ($1, $2, 'salt-ref', $3, 'aggregate', 'enrolment', 'EUR', now())
            RETURNING id
            """,
            business_id,
            "a" * 64,
            convocatoria_id,
        )
        return {
            "business_id": business_id,
            "course_id": course_id,
            "convocatoria_id": convocatoria_id,
            "lead_attribution_id": lead_attribution_id,
        }
    finally:
        await connection.close()


async def _fetchval(dsn: str, query: str, *args: object) -> object:
    connection = await asyncpg.connect(dsn)
    try:
        return await connection.fetchval(query, *args)
    finally:
        await connection.close()


async def test_upgrade_renames_tables_columns_and_rewrites_data(
    postgres_container: PostgresContainer,
) -> None:
    admin_dsn = to_asyncpg_dsn(postgres_container.get_connection_url())
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{_VOCAB_DB}"')
        await admin.execute(f'CREATE DATABASE "{_VOCAB_DB}"')
    finally:
        await admin.close()

    alembic_dsn = with_database(to_alembic_dsn(postgres_container.get_connection_url()), _VOCAB_DB)
    query_dsn = with_database(admin_dsn, _VOCAB_DB)

    await asyncio.to_thread(upgrade, alembic_dsn, "0023_owner_settings")
    ids = await _seed_legacy_rows(query_dsn)

    await asyncio.to_thread(upgrade, alembic_dsn)

    # Tablas y columnas renombradas; la fila sigue accesible bajo el nombre nuevo.
    offering_title = await _fetchval(
        query_dsn, "SELECT title FROM offerings WHERE id = $1", ids["course_id"]
    )
    assert offering_title == "Curso de prueba"

    event_row_name = await _fetchval(
        query_dsn, "SELECT name FROM calendar_events WHERE id = $1", ids["convocatoria_id"]
    )
    assert event_row_name == "Conv 2026"

    kind_default = await _fetchval(
        query_dsn, "SELECT kind FROM calendar_events WHERE id = $1", ids["convocatoria_id"]
    )
    assert kind_default == "season"

    # `enrolment` reescrito a `business_conversion`.
    conversion_kind = await _fetchval(
        query_dsn,
        "SELECT conversion_kind FROM lead_attributions WHERE id = $1",
        ids["lead_attribution_id"],
    )
    assert conversion_kind == "business_conversion"

    # Constraints/indices con el nombre nuevo (verificacion de esquema, no solo de dato).
    connection = await asyncpg.connect(query_dsn)
    try:
        constraint_names = {
            row["conname"]
            for row in await connection.fetch(
                """
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'calendar_events'::regclass
                """
            )
        }
        assert "calendar_events_pkey" in constraint_names
        assert "calendar_events_region_check" in constraint_names
        assert "calendar_events_offering_id_fkey" in constraint_names
        assert "convocatorias_pkey" not in constraint_names

        index_names = {
            row["indexname"]
            for row in await connection.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'calendar_events'"
            )
        }
        assert "ix_calendar_events_business_window" in index_names
        assert "ix_calendar_events_offering" in index_names
    finally:
        await connection.close()

    await asyncio.to_thread(downgrade, alembic_dsn, "0023_owner_settings")

    # Simetria completa: el downgrade deja el dato legible bajo el nombre viejo.
    reverted_title = await _fetchval(
        query_dsn, "SELECT title FROM courses WHERE id = $1", ids["course_id"]
    )
    assert reverted_title == "Curso de prueba"
    reverted_kind = await _fetchval(
        query_dsn,
        "SELECT conversion_kind FROM lead_attributions WHERE id = $1",
        ids["lead_attribution_id"],
    )
    assert reverted_kind == "enrolment"
