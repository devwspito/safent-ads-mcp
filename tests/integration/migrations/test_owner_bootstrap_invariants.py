"""008-mcp-ads-estandar T020: los cuatro puntos de `data-model.md`
§Migration plan, verificados contra el esquema real antes de la fase D.

Tres se comprueban aqui (los de base de datos) y el cuarto —que
`ops/backup.sh` incluya el directorio de estado de topes del broker— vive
en el propio script, que no tiene base de datos que interrogar.

**No hay migracion que escribir**: `owners.email` ya es UNIQUE, la fila de
dueno ya admite ausencia de material TOTP y `decision_log.event_type` es
`text`, no un enum de base de datos (el arbol no tiene un solo `CREATE
TYPE`). Estos tests existen para que deje de ser cierto en voz alta el dia
que alguien lo cambie, no para documentar lo obvio."""

from __future__ import annotations

import json
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration

_INSERT_OWNER = "INSERT INTO owners (email, password_hash) VALUES ($1, $2) RETURNING id"
_SELECT_TOTP_MATERIAL = (
    "SELECT totp_secret_encrypted, totp_confirmed_at FROM owners WHERE id = $1"
)
_INSERT_DECISION = """
    INSERT INTO decision_log (business_id, event_type, actor_kind, payload)
    VALUES (gen_random_uuid(), $1, 'owner', $2::jsonb)
    RETURNING event_type
"""
_SELECT_EVENT_TYPE_COLUMN = """
    SELECT data_type, udt_name
    FROM information_schema.columns
    WHERE table_name = 'decision_log' AND column_name = 'event_type'
"""

# El tipo de evento que la fase D (topes desde el panel) escribira.
_HARD_CAPS_EVENT_TYPE = "account_hard_caps_changed"
_FIXTURE_PASSWORD_HASH = "argon2id$fixture$not-a-real-hash"  # noqa: S105 -- no es un secreto


def _fresh_email() -> str:
    return f"owner-{uuid.uuid4().hex[:10]}@example.com"


class TestOwnerBootstrap:
    async def test_two_owners_cannot_share_an_email(self, pg: asyncpg.Connection) -> None:
        """La carrera de `OwnerBootstrap` («dos operadores se dan de alta a
        la vez en una instalacion limpia») la resuelve la base: gana uno, el
        otro recibe un rechazo explicito."""
        email = _fresh_email()
        await pg.fetchval(_INSERT_OWNER, email, _FIXTURE_PASSWORD_HASH)

        with pytest.raises(asyncpg.UniqueViolationError):
            await pg.fetchval(_INSERT_OWNER, email, _FIXTURE_PASSWORD_HASH)

    async def test_an_owner_row_exists_without_totp_material(
        self, pg: asyncpg.Connection
    ) -> None:
        """Via federada y via contrasena sin segundo factor: ningun `NOT
        NULL` obliga a inventar material TOTP en el alta."""
        owner_id = await pg.fetchval(_INSERT_OWNER, _fresh_email(), _FIXTURE_PASSWORD_HASH)

        row = await pg.fetchrow(_SELECT_TOTP_MATERIAL, owner_id)

        assert row is not None
        assert row["totp_secret_encrypted"] is None
        assert row["totp_confirmed_at"] is None


class TestDecisionLogEventType:
    async def test_event_type_is_free_text_not_a_database_enum(
        self, pg: asyncpg.Connection
    ) -> None:
        """Si fuera un enum de base de datos, la fase D necesitaria una
        migracion antes de poder registrar un cambio de topes."""
        row = await pg.fetchrow(_SELECT_EVENT_TYPE_COLUMN)

        assert row is not None
        assert row["data_type"] == "text"
        assert row["udt_name"] == "text"

    async def test_a_new_event_type_needs_no_migration(self, pg: asyncpg.Connection) -> None:
        stored = await pg.fetchval(
            _INSERT_DECISION, _HARD_CAPS_EVENT_TYPE, json.dumps({"daily_cap_minor": 5000})
        )

        assert stored == _HARD_CAPS_EVENT_TYPE
