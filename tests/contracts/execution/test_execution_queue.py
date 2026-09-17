"""Contrato de `ExecutionQueuePort`: identico para el doble en memoria y para
`SqlExecutionQueue`."""

from __future__ import annotations

from datetime import timedelta

import pytest

from safent_ads.execution.domain.execution_attempt import (
    ExecutionAttempt,
    ExecutionAttemptInvariantError,
    ExecutionStatus,
    build_idempotency_key,
)
from safent_ads.execution.domain.identifiers import ExecutionId
from tests.contracts.execution.conftest import (
    NOW,
    AuthorizedProposal,
    QueueFixture,
    digest,
)


def claimable_attempt(context: AuthorizedProposal) -> ExecutionAttempt:
    """Intento a la espera de trabajador: `CLAIMED` y sin reclamar todavia
    (`started_at is None`)."""
    return ExecutionAttempt(
        execution_id=ExecutionId.new(),
        business_id=context.business_id,
        proposal_id=context.proposal_id,
        authorization_id=context.authorization_id,
        idempotency_key=build_idempotency_key(context.proposal_id, context.diff_hash),
        previous_value=None,
        platform_state_hash_before=context.expected_state_hash,
        attempt_count=0,
    )


async def test_claimed_attempt_carries_its_identity(queue: QueueFixture) -> None:
    context = await queue.given_authorized_proposal()
    await queue.enqueue(claimable_attempt(context))

    claimed = await queue.queue.claim_next()

    assert claimed is not None
    assert claimed.proposal_id == context.proposal_id
    assert claimed.authorization_id == context.authorization_id
    assert claimed.business_id == context.business_id
    assert claimed.idempotency_key == build_idempotency_key(
        context.proposal_id, context.diff_hash
    )
    assert claimed.status is ExecutionStatus.CLAIMED


async def test_a_claimed_attempt_is_never_handed_out_twice(queue: QueueFixture) -> None:
    """Lo que el `SKIP LOCKED` garantiza entre trabajadores, el doble lo
    modela sacando de la cola: reclamado una vez, nadie mas lo ve."""
    context = await queue.given_authorized_proposal()
    await queue.enqueue(claimable_attempt(context))

    first = await queue.queue.claim_next()
    second = await queue.queue.claim_next()

    assert first is not None
    assert second is None


async def test_empty_queue_claims_nothing(queue: QueueFixture) -> None:
    assert await queue.queue.claim_next() is None


async def test_claim_next_scoped_to_a_proposal_skips_an_older_foreign_row(
    queue: QueueFixture,
) -> None:
    """Security review F2/F3, C-2 nit 3: `apply_defensive_action.py` pide su
    PROPIA fila, no la mas antigua de cualquiera. Se encola primero (mas
    antigua) el intento de OTRO negocio y despues el propio; `claim_next(
    proposal_id=...)` debe devolver el propio pese a no ser el mas antiguo,
    y dejar el ajeno intacto y todavia reclamable."""
    older_foreign = await queue.given_authorized_proposal()
    await queue.enqueue(claimable_attempt(older_foreign))
    own = await queue.given_authorized_proposal()
    await queue.enqueue(claimable_attempt(own))

    claimed = await queue.queue.claim_next(proposal_id=own.proposal_id)

    assert claimed is not None
    assert claimed.proposal_id == own.proposal_id

    foreign_still_pending = await queue.queue.get_for_proposal(older_foreign.proposal_id)
    assert foreign_still_pending is not None
    assert foreign_still_pending.status is ExecutionStatus.CLAIMED
    assert foreign_still_pending.started_at is None

    next_unscoped = await queue.queue.claim_next()
    assert next_unscoped is not None
    assert next_unscoped.proposal_id == older_foreign.proposal_id


async def test_unknown_proposal_has_no_attempt(queue: QueueFixture) -> None:
    context = await queue.given_authorized_proposal()
    assert await queue.queue.get_for_proposal(context.proposal_id) is None


async def test_saved_attempt_is_read_back_by_proposal(queue: QueueFixture) -> None:
    context = await queue.given_authorized_proposal()
    attempt = claimable_attempt(context)

    await queue.queue.save(attempt)
    stored = await queue.queue.get_for_proposal(context.proposal_id)

    assert stored is not None
    assert stored.execution_id == attempt.execution_id
    assert stored.status is ExecutionStatus.CLAIMED
    assert stored.platform_state_hash_before == context.expected_state_hash


async def test_terminal_outcome_round_trips(queue: QueueFixture) -> None:
    """El desenlace se guarda entero: estado, valor aplicado, estado remoto
    confirmado y fin de la ventana de deshacer (FR-15)."""
    context = await queue.given_authorized_proposal()
    attempt = claimable_attempt(context)
    await queue.queue.save(attempt)
    state_hash_after = digest(f"after-{context.proposal_id}")
    undo_deadline = NOW + timedelta(minutes=30)

    attempt.start_running()
    attempt.succeed("70", state_hash_after, undo_deadline, NOW)
    await queue.queue.save(attempt)

    stored = await queue.queue.get_for_proposal(context.proposal_id)
    assert stored is not None
    assert stored.status is ExecutionStatus.EXECUTED
    assert stored.applied_value == "70"
    assert stored.platform_state_hash_after == state_hash_after
    assert stored.undo_deadline == undo_deadline
    assert stored.finished_at == NOW


async def test_blocked_by_brake_round_trips(queue: QueueFixture) -> None:
    """Un intento detenido por el freno tambien se persiste: el esquema
    exige motivo para todo desenlace no exitoso y el adaptador lo pone."""
    context = await queue.given_authorized_proposal()
    attempt = claimable_attempt(context)

    attempt.block_by_brake(NOW)
    await queue.queue.save(attempt)

    stored = await queue.queue.get_for_proposal(context.proposal_id)
    assert stored is not None
    assert stored.status is ExecutionStatus.BLOCKED_BRAKE
    assert stored.finished_at == NOW


async def test_guardrail_block_keeps_its_reasons(queue: QueueFixture) -> None:
    context = await queue.given_authorized_proposal()
    attempt = claimable_attempt(context)

    attempt.block_by_guardrail(("daily_cap_exceeded", "max_step"), NOW)
    await queue.queue.save(attempt)

    stored = await queue.queue.get_for_proposal(context.proposal_id)
    assert stored is not None
    assert stored.status is ExecutionStatus.BLOCKED_GUARDRAIL
    assert stored.error_code == "daily_cap_exceeded,max_step"


async def test_undone_marking_round_trips(queue: QueueFixture) -> None:
    """FR-15, bug corregido: `UndoExecution` (execution.application) anota
    el intento ORIGINAL con `undone_at`/`compensating_proposal_id` en la
    misma unidad de trabajo que crea la propuesta compensatoria -- sin
    esto, el panel nunca podia mostrar "deshecha" (data-model.md
    `executions`)."""
    context = await queue.given_authorized_proposal()
    attempt = claimable_attempt(context)
    await queue.queue.save(attempt)
    attempt.start_running()
    attempt.succeed("70", digest(f"after-{context.proposal_id}"), NOW + timedelta(minutes=30), NOW)
    await queue.queue.save(attempt)
    # `compensating_proposal_id` es FK a `proposals.id`: en la variante SQL
    # tiene que apuntar a una fila de verdad, no a cualquier UUID.
    compensating = await queue.given_authorized_proposal()

    attempt.mark_undone(NOW, compensating.proposal_id, reason="restored")
    await queue.queue.save(attempt)

    stored = await queue.queue.get_for_proposal(context.proposal_id)
    assert stored is not None
    assert stored.undone_at == NOW
    assert stored.undo_reason == "restored"
    assert stored.compensating_proposal_id == compensating.proposal_id


async def test_marking_an_already_undone_attempt_is_rejected(queue: QueueFixture) -> None:
    """Solo-anexable: el dominio nunca deja anotar `undone_at` dos veces,
    ni siquiera sobre un intento releido despues de un round-trip real."""
    context = await queue.given_authorized_proposal()
    attempt = claimable_attempt(context)
    await queue.queue.save(attempt)
    attempt.start_running()
    attempt.succeed("70", digest(f"after-{context.proposal_id}"), NOW + timedelta(minutes=30), NOW)
    compensating = await queue.given_authorized_proposal()
    attempt.mark_undone(NOW, compensating.proposal_id, reason="restored")
    await queue.queue.save(attempt)

    stored = await queue.queue.get_for_proposal(context.proposal_id)
    assert stored is not None
    with pytest.raises(ExecutionAttemptInvariantError):
        stored.mark_undone(NOW, compensating.proposal_id, reason="restored")
