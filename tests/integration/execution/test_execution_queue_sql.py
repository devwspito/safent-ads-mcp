"""`SqlExecutionQueue` contra Postgres real: lo que ningun doble en memoria
puede probar — el `FOR UPDATE SKIP LOCKED` entre dos sesiones y el UNIQUE de
`idempotency_key` (threat-model.md C-8/C-15)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.domain.execution_attempt import ExecutionAttempt
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.infrastructure.errors import (
    DuplicateExecutionError,
    ExecutionRowRejectedError,
)
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
from safent_ads.shared.clock import FixedClock
from tests.contracts.execution.conftest import NOW, seed_authorized_proposal
from tests.contracts.execution.test_execution_queue import claimable_attempt
from tests.integration.execution.conftest import committed_scenario

pytestmark = pytest.mark.integration


def queue_for(session: AsyncSession) -> SqlExecutionQueue:
    return SqlExecutionQueue(session, FixedClock(NOW))


async def test_two_workers_never_claim_the_same_execution(isolated_database_url: str) -> None:
    """C-15: el reclamo es exclusivo. Dos sesiones abiertas a la vez piden
    trabajo; la fila se la lleva una sola y la otra no se queda bloqueada
    esperando — `SKIP LOCKED`, no `FOR UPDATE` a secas."""
    async with committed_scenario(isolated_database_url) as scenario:
        async with scenario.session() as setup:
            await queue_for(setup).save(claimable_attempt(scenario.context))
            await setup.commit()

        async with scenario.session() as first, scenario.session() as second:
            claimed_by_first = await queue_for(first).claim_next()
            claimed_by_second = await queue_for(second).claim_next()

            assert claimed_by_first is not None
            assert claimed_by_second is None
            assert claimed_by_first.proposal_id == scenario.context.proposal_id


async def test_a_confirmed_claim_is_not_reclaimable_by_the_next_worker(
    isolated_database_url: str,
) -> None:
    """El candado desaparece al confirmar; la marca de reclamo no. Sin ella,
    el segundo trabajador ejecutaria el mismo cambio mientras el primero
    todavia habla con la plataforma."""
    async with committed_scenario(isolated_database_url) as scenario:
        async with scenario.session() as setup:
            await queue_for(setup).save(claimable_attempt(scenario.context))
            await setup.commit()

        async with scenario.session() as first:
            assert await queue_for(first).claim_next() is not None
            await first.commit()

        async with scenario.session() as second:
            assert await queue_for(second).claim_next() is None


async def test_claim_waits_for_the_undo_grace_to_expire(isolated_session: AsyncSession) -> None:
    """`scheduled_at` sale de `proposals.execution_scheduled_at`: mientras la
    gracia para deshacer no vence, la fila no es reclamable (FR-15)."""
    context = await seed_authorized_proposal(isolated_session)
    await isolated_session.execute(
        text(
            "UPDATE proposals SET execution_scheduled_at = :later WHERE id = :id"
        ),
        {"later": NOW.replace(hour=18), "id": str(context.proposal_id)},
    )
    queue = queue_for(isolated_session)
    await queue.save(claimable_attempt(context))

    assert await queue.claim_next() is None


async def test_retry_no_duplicate_change(isolated_session: AsyncSession) -> None:
    """C-8: reintentar el mismo cambio no crea una segunda ejecucion. La
    frontera es el UNIQUE del esquema, no una comprobacion previa que dos
    trabajadores podrian cruzar."""
    context = await seed_authorized_proposal(isolated_session)
    queue = queue_for(isolated_session)
    first = claimable_attempt(context)
    await queue.save(first)

    retry = ExecutionAttempt(
        execution_id=ExecutionId.new(),
        business_id=context.business_id,
        proposal_id=context.proposal_id,
        authorization_id=context.authorization_id,
        idempotency_key=first.idempotency_key,
        platform_state_hash_before=context.expected_state_hash,
    )

    with pytest.raises(DuplicateExecutionError):
        await queue.save(retry)


async def test_saving_the_same_attempt_twice_updates_it(isolated_session: AsyncSession) -> None:
    """El reintento del MISMO intento (mismo `execution_id`) no duplica fila:
    actualiza la suya. Es lo que hace el chokepoint en cada paso."""
    context = await seed_authorized_proposal(isolated_session)
    queue = queue_for(isolated_session)
    attempt = claimable_attempt(context)
    await queue.save(attempt)

    attempt.fail("platform_write_error:TimeoutError", NOW)
    await queue.save(attempt)

    rows = await isolated_session.execute(
        text("SELECT outcome, error_code FROM executions WHERE proposal_id = :id"),
        {"id": str(context.proposal_id)},
    )
    assert [tuple(row) for row in rows.all()] == [
        ("FAILED", "platform_write_error:TimeoutError")
    ]


async def test_a_state_hash_that_is_not_a_digest_is_rejected(
    isolated_session: AsyncSession,
) -> None:
    """El esquema exige sha256 en `platform_state_hash_after`: un "exito"
    confirmado contra un valor que no es un digest no entra. El adaptador
    lo levanta como fallo tipado, no lo maquilla."""
    context = await seed_authorized_proposal(isolated_session)
    queue = queue_for(isolated_session)
    attempt = claimable_attempt(context)
    await queue.save(attempt)
    attempt.start_running()
    attempt.succeed("70", "no-es-un-sha256", NOW, NOW)

    with pytest.raises(ExecutionRowRejectedError):
        await queue.save(attempt)
