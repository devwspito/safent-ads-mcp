"""`UndoExecution` (T070; FR-15)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from safent_ads.execution.application.undo_execution import (
    ExecutionAlreadyUndoneError,
    UndoExecution,
    UndoExecutionCommand,
    UndoNotAllowedError,
    UndoOutcome,
)
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, build_idempotency_key
from safent_ads.execution.domain.guardrails import (
    GuardrailChange,
    GuardrailEvaluator,
    GuardrailSet,
    money_pair_from_diff,
)
from safent_ads.execution.testing.fakes import (
    FakeExecutionQueuePort,
    FakeGuardrailSetRepository,
    FakeSpendLedger,
    FakeUnitOfWork,
)
from safent_ads.proposals.domain.authorization import AuthorizationKind
from safent_ads.proposals.domain.identifiers import AuthorizationId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import ProposalState, new_proposal_id
from safent_ads.proposals.testing.fakes import (
    FakeAuthorizationRepository,
    FakeProposalRepository,
    FakeSignerPort,
)
from safent_ads.shared.clock import FixedClock

from .conftest import (
    NOW,
    empty_ledger,
    entity_scope,
    guardrail_set,
    make_pending_proposal,
    make_scheduled_proposal,
)

_SCOPE = entity_scope()


async def _build_executed_scenario(
    *, undo_deadline_offset: timedelta, guardrails: GuardrailSet | None = None
):
    proposal = make_scheduled_proposal()
    proposal.begin_execution(NOW)
    proposal.record_execution(success=True, now=NOW)
    proposal.pull_events()

    attempt = ExecutionAttempt.claim(
        proposal.business_id,
        proposal.proposal_id,
        AuthorizationId.new(),
        proposal.diff.diff_hash,
        NOW,
    )
    attempt.start_running()
    attempt.succeed("70.00", "state-after", NOW + undo_deadline_offset, NOW)

    proposals = FakeProposalRepository()
    await proposals.save(proposal)
    authorizations = FakeAuthorizationRepository()
    executions = FakeExecutionQueuePort()
    await executions.save(attempt)

    use_case = UndoExecution(
        proposals=proposals,
        authorizations=authorizations,
        executions=executions,
        uow=FakeUnitOfWork(),
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=FakeGuardrailSetRepository({_SCOPE.ref: guardrails or guardrail_set()}),
        spend_ledger=FakeSpendLedger({_SCOPE.ref: empty_ledger()}),
        signer=FakeSignerPort(),
        clock=FixedClock(NOW),
    )
    return use_case, proposal, proposals, authorizations


class TestUndoWithinGraceRestoresValue:
    async def test_undo_within_grace_restores_value(self) -> None:
        use_case, original, proposals, authorizations = await _build_executed_scenario(
            undo_deadline_offset=timedelta(minutes=30)
        )

        result = await use_case.execute(
            UndoExecutionCommand(original.proposal_id, initiated_by="owner-1")
        )

        assert result.outcome is UndoOutcome.RESTORED
        restore_candidates = [
            p
            for p in proposals.all()
            if p.proposal_id != original.proposal_id
        ]
        assert len(restore_candidates) == 1
        restore_proposal = restore_candidates[0]
        assert restore_proposal.diff.after == original.diff.before
        assert restore_proposal.diff.before == original.diff.after
        assert restore_proposal.state is ProposalState.SCHEDULED
        assert result.compensating_proposal_id == restore_proposal.proposal_id

        authorization = await authorizations.get_active_for_proposal(restore_proposal.proposal_id)
        assert authorization is not None
        assert authorization.kind is AuthorizationKind.HUMAN_APPROVAL
        assert authorization.issued_by == "owner-1"

    async def test_restore_enqueues_an_execution_row(self) -> None:
        """Gap del carril exec-SQL: sin una fila de `executions` nueva, el
        `ExecutionCycle` no tiene nada que reclamar y "Deshacer" nunca se
        aplicaria de verdad (solo quedaria la propuesta en `SCHEDULED`)."""
        use_case, original, proposals, _authorizations = await _build_executed_scenario(
            undo_deadline_offset=timedelta(minutes=30)
        )
        executions: FakeExecutionQueuePort = use_case._executions  # noqa: SLF001

        await use_case.execute(UndoExecutionCommand(original.proposal_id, initiated_by="owner-1"))

        restore_proposal = next(p for p in proposals.all() if p.proposal_id != original.proposal_id)
        enqueued = await executions.get_for_proposal(restore_proposal.proposal_id)
        assert enqueued is not None
        assert enqueued.platform_state_hash_before == "state-after"
        assert enqueued.idempotency_key == build_idempotency_key(
            restore_proposal.proposal_id, restore_proposal.diff.diff_hash
        )

    async def test_restore_authorization_diff_hash_matches_restore_proposal(self) -> None:
        # `max_step_pct` ancho a proposito: la restauracion por defecto (70
        # -> 100, +42,86%) supera el 30% del guardarraíl por defecto de este
        # modulo -- este test comprueba la igualdad SIN recorte (ver el
        # siguiente test para el caso CON recorte, `effective_diff`).
        wide_guardrails = guardrail_set(max_step_pct=0.5)
        use_case, original, proposals, authorizations = await _build_executed_scenario(
            undo_deadline_offset=timedelta(minutes=30), guardrails=wide_guardrails
        )

        await use_case.execute(UndoExecutionCommand(original.proposal_id, initiated_by="owner-1"))

        restore_proposal = next(
            p
            for p in proposals.all()
            if p.proposal_id != original.proposal_id
        )
        authorization = await authorizations.get_active_for_proposal(restore_proposal.proposal_id)
        assert authorization is not None
        assert authorization.diff_hash == restore_proposal.diff.diff_hash

    async def test_restore_authorization_signs_the_clamped_diff_when_guardrail_clamps(
        self,
    ) -> None:
        """BUG corregido (T3, execution/domain/guardrails.py): un `deshacer`
        que cruza el salto maximo del guardarraíl se recorta como cualquier
        otro cambio -- la autorizacion firma el diff EFECTIVO (recortado),
        nunca `restore_proposal.diff.diff_hash` a secas (INV-1: la propuesta
        compensatoria en si sigue sin mutar)."""
        use_case, original, proposals, authorizations = await _build_executed_scenario(
            undo_deadline_offset=timedelta(minutes=30)
        )

        await use_case.execute(UndoExecutionCommand(original.proposal_id, initiated_by="owner-1"))

        restore_proposal = next(
            p
            for p in proposals.all()
            if p.proposal_id != original.proposal_id
        )
        authorization = await authorizations.get_active_for_proposal(restore_proposal.proposal_id)
        assert authorization is not None
        # INV-1: `restore_proposal.diff` NUNCA se muta -- sigue siendo el
        # pedido de deshacer sin recortar (70 -> 100).
        assert restore_proposal.diff.after == Money.of("100")
        # Recorte esperado: 70 + 30% (max_step_pct por defecto) -- se
        # recalcula con el mismo `GuardrailEvaluator` en vez de fijar a mano
        # el importe exacto, para no depender del redondeo interno de
        # `Money.scaled_by`.
        before, after = money_pair_from_diff(restore_proposal.diff)
        verdict = GuardrailEvaluator().evaluate(
            GuardrailChange(
                scope=_SCOPE,
                entity_ref=restore_proposal.diff.entity_ref,
                authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
                before=before,
                after=after,
            ),
            guardrail_set(),
            empty_ledger(),
        )
        expected_diff_hash = restore_proposal.diff.with_new_value(verdict.clamped_after).diff_hash
        assert authorization.diff_hash == expected_diff_hash
        assert authorization.diff_hash != restore_proposal.diff.diff_hash


class TestUndoOutsideGraceCreatesCompensatingProposal:
    async def test_outside_grace_creates_pending_compensating_proposal(self) -> None:
        use_case, original, proposals, authorizations = await _build_executed_scenario(
            undo_deadline_offset=-timedelta(minutes=1)
        )

        result = await use_case.execute(
            UndoExecutionCommand(original.proposal_id, initiated_by="owner-1")
        )

        assert result.outcome is UndoOutcome.COMPENSATING_PROPOSAL_CREATED
        compensating = next(
            p
            for p in proposals.all()
            if p.proposal_id != original.proposal_id
        )
        assert compensating.state is ProposalState.PENDING
        assert await authorizations.get_active_for_proposal(compensating.proposal_id) is None
        assert result.compensating_proposal_id == compensating.proposal_id


class TestUndoScheduledCancels:
    async def test_undo_scheduled_cancels_without_touching_platform(self) -> None:
        proposal = make_scheduled_proposal(grace_period_seconds=1800)
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        use_case = UndoExecution(
            proposals=proposals,
            authorizations=FakeAuthorizationRepository(),
            executions=FakeExecutionQueuePort(),
            uow=FakeUnitOfWork(),
            guardrail_evaluator=GuardrailEvaluator(),
            guardrail_sets=FakeGuardrailSetRepository({_SCOPE.ref: guardrail_set()}),
            spend_ledger=FakeSpendLedger({_SCOPE.ref: empty_ledger()}),
            signer=FakeSignerPort(),
            clock=FixedClock(NOW),
        )

        result = await use_case.execute(
            UndoExecutionCommand(proposal.proposal_id, initiated_by="owner-1")
        )

        assert result.outcome is UndoOutcome.CANCELLED_SCHEDULED
        assert result.compensating_proposal_id is None
        saved = await proposals.get(proposal.proposal_id)
        assert saved is not None
        assert saved.state is ProposalState.INVALIDATED


class TestUndoNotAllowed:
    async def test_proposal_not_found_raises(self) -> None:
        use_case, original, _proposals, _authorizations = await _build_executed_scenario(
            undo_deadline_offset=timedelta(minutes=30)
        )

        with pytest.raises(UndoNotAllowedError):
            await use_case.execute(UndoExecutionCommand(new_proposal_id(), initiated_by="owner-1"))

    async def test_pending_proposal_has_nothing_to_undo(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        use_case = UndoExecution(
            proposals=proposals,
            authorizations=FakeAuthorizationRepository(),
            executions=FakeExecutionQueuePort(),
            uow=FakeUnitOfWork(),
            guardrail_evaluator=GuardrailEvaluator(),
            guardrail_sets=FakeGuardrailSetRepository({_SCOPE.ref: guardrail_set()}),
            spend_ledger=FakeSpendLedger({_SCOPE.ref: empty_ledger()}),
            signer=FakeSignerPort(),
            clock=FixedClock(NOW),
        )

        with pytest.raises(UndoNotAllowedError):
            await use_case.execute(
                UndoExecutionCommand(proposal.proposal_id, initiated_by="owner-1")
            )


class TestUndoMarksTheOriginalExecutionAttempt:
    """El bug que reporta esta rama: sin esto, `executions.undone_at` y
    `compensating_proposal_id` nunca se escriben -- el panel no puede
    mostrar "deshecha" y nada impide deshacer la misma ejecucion dos veces."""

    async def test_undo_within_grace_marks_the_original_attempt(self) -> None:
        use_case, original, _proposals, _authorizations = await _build_executed_scenario(
            undo_deadline_offset=timedelta(minutes=30)
        )
        executions: FakeExecutionQueuePort = use_case._executions  # noqa: SLF001

        result = await use_case.execute(
            UndoExecutionCommand(original.proposal_id, initiated_by="owner-1")
        )

        original_attempt = await executions.get_for_proposal(original.proposal_id)
        assert original_attempt is not None
        assert original_attempt.undone_at == NOW
        assert original_attempt.compensating_proposal_id == result.compensating_proposal_id

    async def test_undo_outside_grace_marks_the_original_attempt(self) -> None:
        use_case, original, _proposals, _authorizations = await _build_executed_scenario(
            undo_deadline_offset=-timedelta(minutes=1)
        )
        executions: FakeExecutionQueuePort = use_case._executions  # noqa: SLF001

        result = await use_case.execute(
            UndoExecutionCommand(original.proposal_id, initiated_by="owner-1")
        )

        original_attempt = await executions.get_for_proposal(original.proposal_id)
        assert original_attempt is not None
        assert original_attempt.undone_at == NOW
        assert original_attempt.compensating_proposal_id == result.compensating_proposal_id


class TestUndoIsIdempotent:
    async def test_undoing_an_already_undone_execution_raises_a_typed_conflict(self) -> None:
        use_case, original, proposals, _authorizations = await _build_executed_scenario(
            undo_deadline_offset=timedelta(minutes=30)
        )
        await use_case.execute(UndoExecutionCommand(original.proposal_id, initiated_by="owner-1"))

        with pytest.raises(ExecutionAlreadyUndoneError):
            await use_case.execute(
                UndoExecutionCommand(original.proposal_id, initiated_by="owner-1")
            )

        # Idempotencia real: el segundo "Deshacer" no fabrica una segunda
        # propuesta compensatoria.
        compensating = [p for p in proposals.all() if p.proposal_id != original.proposal_id]
        assert len(compensating) == 1
