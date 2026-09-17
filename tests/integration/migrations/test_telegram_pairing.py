"""0022_telegram_pairing: `telegram_owner_chats` (un emparejamiento por
propietario, un chat `paired` no puede pertenecer a dos propietarios a la
vez, `paired` exige `chat_id`+`verified_at`), `telegram_pairing_attempts`
(limite de intentos) y `telegram_test_messages` (cola del mensaje de
prueba)."""

from __future__ import annotations

import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.integration

_INSERT_OWNER = """
    INSERT INTO owners (id, email, password_hash)
    VALUES ($1, $2, 'argon2id$fixture$not-a-real-hash')
"""


async def _make_owner(pg: asyncpg.Connection) -> uuid.UUID:
    owner_id = uuid.uuid4()
    await pg.execute(_INSERT_OWNER, owner_id, f"owner-{owner_id.hex[:8]}@safent.example")
    return owner_id


def _fresh_chat_id() -> int:
    """El `pg` fixture no envuelve nada en una transaccion que se deshaga
    (a diferencia de `db_session`): una fila `paired` que este archivo
    inserta sobrevive al test que la creo. `ix_telegram_owner_chats_paired_chat`
    (0021) es UNIQUE de verdad, asi que reutilizar una constante fija de
    `chat_id` entre tests (o entre ficheros) choca contra la fila que dejo
    el anterior -- cada test necesita la suya."""
    return 100_000_000 + (uuid.uuid4().int % 800_000_000)


async def test_one_pairing_row_per_owner(pg: asyncpg.Connection) -> None:
    owner_id = await _make_owner(pg)
    await pg.execute(
        "INSERT INTO telegram_owner_chats (owner_id, status) VALUES ($1, 'unpaired')", owner_id
    )

    with pytest.raises(asyncpg.UniqueViolationError, match="telegram_owner_chats_owner_id_key"):
        await pg.execute(
            "INSERT INTO telegram_owner_chats (owner_id, status) VALUES ($1, 'unpaired')",
            owner_id,
        )


async def test_paired_status_requires_chat_id_and_verified_at(pg: asyncpg.Connection) -> None:
    owner_id = await _make_owner(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="telegram_owner_chats_paired_has_chat"):
        await pg.execute(
            "INSERT INTO telegram_owner_chats (owner_id, status) VALUES ($1, 'paired')", owner_id
        )


async def test_paired_status_with_chat_and_verified_at_is_accepted(
    pg: asyncpg.Connection,
) -> None:
    owner_id = await _make_owner(pg)

    await pg.execute(
        "INSERT INTO telegram_owner_chats (owner_id, status, chat_id, verified_at) "
        "VALUES ($1, 'paired', $2, now())",
        owner_id,
        _fresh_chat_id(),
    )


async def test_a_chat_cannot_be_paired_to_two_owners_at_once(pg: asyncpg.Connection) -> None:
    chat_id = _fresh_chat_id()
    first_owner = await _make_owner(pg)
    second_owner = await _make_owner(pg)
    await pg.execute(
        "INSERT INTO telegram_owner_chats (owner_id, status, chat_id, verified_at) "
        "VALUES ($1, 'paired', $2, now())",
        first_owner,
        chat_id,
    )

    with pytest.raises(asyncpg.UniqueViolationError, match="ix_telegram_owner_chats_paired_chat"):
        await pg.execute(
            "INSERT INTO telegram_owner_chats (owner_id, status, chat_id, verified_at) "
            "VALUES ($1, 'paired', $2, now())",
            second_owner,
            chat_id,
        )


async def test_status_is_constrained_to_the_three_known_values(pg: asyncpg.Connection) -> None:
    owner_id = await _make_owner(pg)

    with pytest.raises(asyncpg.CheckViolationError):
        await pg.execute(
            "INSERT INTO telegram_owner_chats (owner_id, status) VALUES ($1, 'bogus')", owner_id
        )


async def test_pairing_attempts_count_within_a_window(pg: asyncpg.Connection) -> None:
    chat_id = _fresh_chat_id()
    for _ in range(3):
        await pg.execute(
            "INSERT INTO telegram_pairing_attempts (chat_id, succeeded) VALUES ($1, false)",
            chat_id,
        )

    count = await pg.fetchval(
        "SELECT COUNT(*) FROM telegram_pairing_attempts "
        "WHERE chat_id = $1 AND attempted_at >= now() - interval '1 hour'",
        chat_id,
    )
    assert count == 3


async def test_test_message_delivery_state_is_constrained(pg: asyncpg.Connection) -> None:
    owner_id = await _make_owner(pg)

    with pytest.raises(asyncpg.CheckViolationError):
        await pg.execute(
            "INSERT INTO telegram_test_messages (owner_id, chat_id, body, delivery_state) "
            "VALUES ($1, 111222333, 'hola', 'BOGUS')",
            owner_id,
        )


async def test_test_message_defaults_to_pending(pg: asyncpg.Connection) -> None:
    owner_id = await _make_owner(pg)

    row_id = await pg.fetchval(
        "INSERT INTO telegram_test_messages (owner_id, chat_id, body) "
        "VALUES ($1, 111222333, 'hola') RETURNING id",
        owner_id,
    )

    state = await pg.fetchval(
        "SELECT delivery_state FROM telegram_test_messages WHERE id = $1", row_id
    )
    assert state == "PENDING"
