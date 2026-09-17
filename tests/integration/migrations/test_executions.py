"""0009_executions: reintentar no duplica el cambio ni el aviso, y el
ledger sostiene los topes diario y mensual (C-8, C-17)."""

from __future__ import annotations

import datetime as dt
import uuid

import asyncpg
import pytest

from tests.integration.migrations.conftest import (
    make_approval,
    make_business,
    make_entity,
    make_platform_account,
    make_proposal,
    state_hash,
)

pytestmark = pytest.mark.integration

_CLAIM_EXECUTION = """
    INSERT INTO executions (proposal_id, authorization_id, business_id, entity_ref,
                            idempotency_key, previous_value, platform_state_hash_before)
    VALUES ($1, $2, $3, $4, $5, '{"amount": 60}'::jsonb, $6)
    ON CONFLICT ON CONSTRAINT executions_idempotency_key_unique DO NOTHING
    RETURNING id
"""

_NOTIFY = """
    INSERT INTO notifications (business_id, channel, severity, kind, payload, dedupe_key)
    VALUES ($1, 'telegram', 'NORMAL', 'auto_receipt', '{"linea": "bajada aplicada"}'::jsonb, $2)
    ON CONFLICT ON CONSTRAINT notifications_dedupe_key_unique DO NOTHING
"""

_LEDGER_CHANGE = """
    INSERT INTO spend_ledger (business_id, platform_account_id, entity_ref, ledger_date,
                              currency, kind, delta_minor, previous_value_minor,
                              new_value_minor, execution_id)
    VALUES ($1, $2, $3, $4, 'EUR', 'applied_change', $5, $6, $7, $8)
"""


async def _authorized_proposal(
    pg: asyncpg.Connection,
) -> tuple[uuid.UUID, uuid.UUID, str, uuid.UUID, uuid.UUID, str]:
    business_id = await make_business(pg)
    account_id = await make_platform_account(pg, business_id)
    entity = await make_entity(pg, business_id, account_id)
    proposal = await make_proposal(pg, business_id, entity["entity_ref"])
    approval_id = await make_approval(pg, proposal["id"], proposal["diff_hash"])
    return (
        business_id,
        account_id,
        entity["entity_ref"],
        proposal["id"],
        approval_id,
        proposal["diff_hash"],
    )


async def test_retry_no_duplicate_change_or_notification(pg: asyncpg.Connection) -> None:
    (
        business_id,
        _account,
        entity_ref,
        proposal_id,
        approval_id,
        diff_hash,
    ) = await _authorized_proposal(pg)
    idempotency_key = f"exec-{proposal_id}-{diff_hash[:12]}"
    dedupe_key = f"auto_receipt:{proposal_id}:{diff_hash[:12]}"
    arguments = (
        proposal_id,
        approval_id,
        business_id,
        entity_ref,
        idempotency_key,
        state_hash("remote-state-before"),
    )

    first = await pg.fetchval(_CLAIM_EXECUTION, *arguments)
    retry = await pg.fetchval(_CLAIM_EXECUTION, *arguments)
    await pg.execute(_NOTIFY, business_id, dedupe_key)
    await pg.execute(_NOTIFY, business_id, dedupe_key)

    executions = await pg.fetchval(
        "SELECT count(*) FROM executions WHERE idempotency_key = $1", idempotency_key
    )
    notifications = await pg.fetchval(
        "SELECT count(*) FROM notifications WHERE dedupe_key = $1", dedupe_key
    )
    assert first is not None
    assert retry is None
    assert executions == 1
    assert notifications == 1


async def test_idempotency_key_collision_is_rejected(pg: asyncpg.Connection) -> None:
    (
        business_id,
        _account,
        entity_ref,
        proposal_id,
        approval_id,
        diff_hash,
    ) = await _authorized_proposal(pg)
    statement = _CLAIM_EXECUTION.replace(
        "ON CONFLICT ON CONSTRAINT executions_idempotency_key_unique DO NOTHING", ""
    )
    arguments = (
        proposal_id,
        approval_id,
        business_id,
        entity_ref,
        f"exec-{proposal_id}-{diff_hash[:12]}",
        state_hash("remote-state-before"),
    )
    await pg.fetchval(statement, *arguments)

    with pytest.raises(asyncpg.UniqueViolationError, match="idempotency_key_unique"):
        await pg.fetchval(statement, *arguments)


async def test_success_must_carry_the_verified_remote_state(pg: asyncpg.Connection) -> None:
    (
        business_id,
        _account,
        entity_ref,
        proposal_id,
        approval_id,
        diff_hash,
    ) = await _authorized_proposal(pg)
    execution_id = await pg.fetchval(
        _CLAIM_EXECUTION,
        proposal_id,
        approval_id,
        business_id,
        entity_ref,
        f"exec-{proposal_id}-{diff_hash[:12]}",
        state_hash("remote-state-before"),
    )

    with pytest.raises(asyncpg.CheckViolationError, match="success_is_verified"):
        await pg.execute(
            "UPDATE executions SET outcome = 'SUCCEEDED', finished_at = now() WHERE id = $1",
            execution_id,
        )


async def test_drift_skip_needs_an_error_code(pg: asyncpg.Connection) -> None:
    (
        business_id,
        _account,
        entity_ref,
        proposal_id,
        approval_id,
        diff_hash,
    ) = await _authorized_proposal(pg)
    execution_id = await pg.fetchval(
        _CLAIM_EXECUTION,
        proposal_id,
        approval_id,
        business_id,
        entity_ref,
        f"exec-{proposal_id}-{diff_hash[:12]}",
        state_hash("remote-state-before"),
    )

    with pytest.raises(asyncpg.CheckViolationError, match="failure_has_code"):
        await pg.execute(
            "UPDATE executions SET outcome = 'SKIPPED_DRIFT', finished_at = now() WHERE id = $1",
            execution_id,
        )

    await pg.execute(
        """
        UPDATE executions
           SET outcome = 'SKIPPED_DRIFT', finished_at = now(), error_code = 'STATE_DRIFT'
         WHERE id = $1
        """,
        execution_id,
    )


async def test_claim_index_targets_the_claimable_rows(pg: asyncpg.Connection) -> None:
    definition = await pg.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_executions_claim'"
    )

    assert definition is not None
    assert "(outcome, scheduled_at)" in definition
    assert (
        "WHERE (outcome = ANY (ARRAY['CLAIMED'::text, 'RUNNING'::text, 'UNKNOWN'::text]))"
        in definition
    )


async def test_ledger_delta_must_match_the_applied_values(pg: asyncpg.Connection) -> None:
    (
        business_id,
        account_id,
        entity_ref,
        proposal_id,
        approval_id,
        diff_hash,
    ) = await _authorized_proposal(pg)
    execution_id = await pg.fetchval(
        _CLAIM_EXECUTION,
        proposal_id,
        approval_id,
        business_id,
        entity_ref,
        f"exec-{proposal_id}-{diff_hash[:12]}",
        state_hash("remote-state-before"),
    )

    with pytest.raises(asyncpg.CheckViolationError, match="applied_change_check"):
        await pg.execute(
            _LEDGER_CHANGE,
            business_id,
            account_id,
            entity_ref,
            dt.date.today(),
            9999,
            6000,
            7800,
            execution_id,
        )


async def test_daily_cap_sums_changes_and_reported_spend(pg: asyncpg.Connection) -> None:
    (
        business_id,
        account_id,
        entity_ref,
        proposal_id,
        approval_id,
        diff_hash,
    ) = await _authorized_proposal(pg)
    execution_id = await pg.fetchval(
        _CLAIM_EXECUTION,
        proposal_id,
        approval_id,
        business_id,
        entity_ref,
        f"exec-{proposal_id}-{diff_hash[:12]}",
        state_hash("remote-state-before"),
    )
    today = dt.date.today()
    await pg.execute(
        _LEDGER_CHANGE, business_id, account_id, entity_ref, today, 1800, 6000, 7800, execution_id
    )
    await pg.execute(
        """
        INSERT INTO spend_ledger (business_id, platform_account_id, entity_ref, ledger_date,
                                  currency, kind, delta_minor, reported_spend_minor)
        VALUES ($1, $2, $3, $4, 'EUR', 'platform_spend', 5400, 5400)
        """,
        business_id,
        account_id,
        entity_ref,
        today,
    )

    total = await pg.fetchval(
        """
        SELECT sum(delta_minor) FROM spend_ledger
         WHERE platform_account_id = $1 AND ledger_date = $2
        """,
        account_id,
        today,
    )
    changes_today = await pg.fetchval(
        """
        SELECT count(*) FROM spend_ledger
         WHERE entity_ref = $1 AND ledger_date = $2 AND kind = 'applied_change'
        """,
        entity_ref,
        today,
    )

    assert total == 7200
    assert changes_today == 1


async def test_one_ledger_entry_per_execution(pg: asyncpg.Connection) -> None:
    (
        business_id,
        account_id,
        entity_ref,
        proposal_id,
        approval_id,
        diff_hash,
    ) = await _authorized_proposal(pg)
    execution_id = await pg.fetchval(
        _CLAIM_EXECUTION,
        proposal_id,
        approval_id,
        business_id,
        entity_ref,
        f"exec-{proposal_id}-{diff_hash[:12]}",
        state_hash("remote-state-before"),
    )
    today = dt.date.today()
    await pg.execute(
        _LEDGER_CHANGE, business_id, account_id, entity_ref, today, 1800, 6000, 7800, execution_id
    )

    with pytest.raises(asyncpg.UniqueViolationError, match="ix_spend_ledger_execution"):
        await pg.execute(
            _LEDGER_CHANGE,
            business_id,
            account_id,
            entity_ref,
            today,
            1800,
            6000,
            7800,
            execution_id,
        )
