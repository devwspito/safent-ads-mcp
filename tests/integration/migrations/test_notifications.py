"""0010_notifications: dedupe de entrega y nonce de un solo uso."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import asyncpg
import pytest

from tests.integration.migrations.conftest import (
    make_business,
    make_entity,
    make_platform_account,
    make_proposal,
)

pytestmark = pytest.mark.integration

_INSERT_CALLBACK = """
    INSERT INTO telegram_callbacks (nonce, proposal_id, chat_id, message_id, diff_hash,
                                    action, expires_at)
    VALUES ($1, $2, 111222333, 42, $3, 'a', now() + interval '6 hours')
    RETURNING id
"""


def _diff_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


async def _proposal_id(pg: asyncpg.Connection) -> uuid.UUID:
    """El nonce cuelga de una propuesta real: `proposal_id` tiene FK desde
    que 0010 va detras de 0008."""
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)
    proposal = await make_proposal(pg, business_id, entity["entity_ref"])
    return proposal["id"]


async def test_nonce_unique(pg: asyncpg.Connection) -> None:
    nonce = uuid.uuid4().hex[:10]
    diff_hash = _diff_hash("propuesta-1")
    proposal_id = await _proposal_id(pg)
    await pg.fetchval(_INSERT_CALLBACK, nonce, proposal_id, diff_hash)

    with pytest.raises(asyncpg.UniqueViolationError, match="telegram_callbacks_nonce_unique"):
        await pg.fetchval(_INSERT_CALLBACK, nonce, proposal_id, diff_hash)


async def test_consumed_nonce_cannot_be_consumed_again(pg: asyncpg.Connection) -> None:
    nonce = uuid.uuid4().hex[:10]
    callback_id = await pg.fetchval(
        _INSERT_CALLBACK, nonce, await _proposal_id(pg), _diff_hash("propuesta-2")
    )

    consumed = await pg.fetchval(
        """
        UPDATE telegram_callbacks SET consumed_at = now()
        WHERE nonce = $1 AND consumed_at IS NULL AND expires_at > now()
        RETURNING id
        """,
        nonce,
    )
    assert consumed == callback_id

    with pytest.raises(asyncpg.RaiseError, match="ya fue consumido"):
        await pg.execute(
            "UPDATE telegram_callbacks SET consumed_at = now() WHERE nonce = $1", nonce
        )


async def test_nonce_shape_is_enforced(pg: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="nonce"):
        await pg.fetchval(
            _INSERT_CALLBACK,
            "nonce con espacios",
            await _proposal_id(pg),
            _diff_hash("propuesta-3"),
        )


async def test_dedupe_key_blocks_a_second_delivery(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)
    dedupe_key = f"ticker:{uuid.uuid4()}"
    statement = """
        INSERT INTO notifications (business_id, channel, severity, kind, payload, dedupe_key)
        VALUES ($1, 'telegram', 'DIGEST', 'ticker', '{"lines": 3}'::jsonb, $2)
    """
    await pg.execute(statement, business_id, dedupe_key)

    with pytest.raises(asyncpg.UniqueViolationError, match="notifications_dedupe_key_unique"):
        await pg.execute(statement, business_id, dedupe_key)


async def test_sent_notification_must_carry_its_message(pg: asyncpg.Connection) -> None:
    business_id = await make_business(pg)

    with pytest.raises(asyncpg.CheckViolationError, match="sent_is_traceable"):
        await pg.execute(
            """
            INSERT INTO notifications (business_id, channel, severity, kind, payload,
                                       dedupe_key, delivery_state, sent_at)
            VALUES ($1, 'telegram', 'CRITICAL', 'critical', '{}'::jsonb, $2, 'SENT', now())
            """,
            business_id,
            f"critical:{uuid.uuid4()}",
        )


async def test_expired_callbacks_are_purgeable(pg: asyncpg.Connection) -> None:
    nonce = uuid.uuid4().hex[:10]
    await pg.execute(
        """
        INSERT INTO telegram_callbacks (nonce, proposal_id, chat_id, message_id, diff_hash,
                                        action, expires_at, created_at)
        VALUES ($1, $2, 111222333, 43, $3, 'a', $4, $5)
        """,
        nonce,
        await _proposal_id(pg),
        _diff_hash("propuesta-4"),
        dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
        dt.datetime.now(dt.UTC) - dt.timedelta(hours=7),
    )

    deleted = await pg.fetchval(
        "DELETE FROM telegram_callbacks WHERE expires_at < now() RETURNING nonce"
    )

    assert deleted == nonce
