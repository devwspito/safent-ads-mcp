"""`SqlExecutionReadPort` contra Postgres real: el join con `proposals`/
`ad_entities` y el mapeo `outcome`/`applied_value`/`previous_value` que
ningun doble en memoria puede probar (0009_executions)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.domain.execution_attempt import ExecutionAttempt
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
from safent_ads.execution.infrastructure.sql_execution_read_port import SqlExecutionReadPort
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.clock import FixedClock
from tests.contracts.execution.conftest import NOW, seed_authorized_proposal
from tests.contracts.execution.test_execution_queue import claimable_attempt

pytestmark = pytest.mark.integration


def _queue(session: AsyncSession) -> SqlExecutionQueue:
    return SqlExecutionQueue(session, FixedClock(NOW))


async def test_get_maps_a_claimed_execution(db_session: AsyncSession) -> None:
    context = await seed_authorized_proposal(db_session)
    attempt = claimable_attempt(context)
    await _queue(db_session).save(attempt)

    view = await SqlExecutionReadPort(db_session).get(str(attempt.execution_id))

    assert view is not None
    assert view.execution_id == str(attempt.execution_id)
    assert view.proposal_id == str(context.proposal_id)
    assert view.business_id == str(context.business_id)
    assert view.entity_name == "Campana de contrato"
    assert view.outcome == "CLAIMED"
    assert view.applied_value is None
    assert view.previous_value is None
    assert view.estimated_impact.amount == 310
    assert view.estimated_impact.currency == "EUR"
    # Nada reclamado todavia: `started_at` cae al `scheduled_at` de la
    # propuesta (COALESCE, ver docstring del adaptador).
    assert view.started_at == NOW


async def test_get_maps_a_succeeded_execution_with_applied_and_previous_values(
    db_session: AsyncSession,
) -> None:
    context = await seed_authorized_proposal(db_session)
    attempt = ExecutionAttempt.claim(
        context.business_id,
        context.proposal_id,
        context.authorization_id,
        context.diff_hash,
        NOW,
    )
    attempt.platform_state_hash_before = context.expected_state_hash
    attempt.previous_value = Money.of("100")
    attempt.start_running()
    attempt.succeed(
        applied_value=Money.of("70"),
        state_hash_after="b" * 64,
        undo_deadline=NOW + timedelta(hours=1),
        now=NOW,
    )
    await _queue(db_session).save(attempt)

    view = await SqlExecutionReadPort(db_session).get(str(attempt.execution_id))

    assert view is not None
    assert view.outcome == "SUCCEEDED"
    assert view.applied_value == 70.0
    assert view.previous_value == 100.0
    assert view.undo_deadline == NOW + timedelta(hours=1)
    assert view.finished_at == NOW


async def test_get_returns_none_for_an_unknown_execution_id(db_session: AsyncSession) -> None:
    unknown_id = "00000000-0000-0000-0000-000000000000"

    assert await SqlExecutionReadPort(db_session).get(unknown_id) is None


class TestListForBusiness:
    async def test_filters_by_outcome(self, db_session: AsyncSession) -> None:
        claimed_context = await seed_authorized_proposal(db_session)
        claimed = claimable_attempt(claimed_context)
        await _queue(db_session).save(claimed)

        failed_context = await seed_authorized_proposal(db_session)
        failed = claimable_attempt(failed_context)
        failed.start_running()
        failed.fail("broker_timeout", NOW)
        await _queue(db_session).save(failed)

        views = await SqlExecutionReadPort(db_session).list_for_business(
            str(claimed_context.business_id), outcome="CLAIMED", since=None, limit=50
        )

        assert [view.execution_id for view in views] == [str(claimed.execution_id)]

    async def test_filters_by_business(self, db_session: AsyncSession) -> None:
        context_a = await seed_authorized_proposal(db_session)
        attempt_a = claimable_attempt(context_a)
        await _queue(db_session).save(attempt_a)

        context_b = await seed_authorized_proposal(db_session)
        attempt_b = claimable_attempt(context_b)
        await _queue(db_session).save(attempt_b)

        views = await SqlExecutionReadPort(db_session).list_for_business(
            str(context_a.business_id), outcome=None, since=None, limit=50
        )

        assert [view.execution_id for view in views] == [str(attempt_a.execution_id)]
