"""`ProposalApprovalGateway` (contracts/telegram.md: "Telegram debe llamar
al MISMO caso de uso que REST, nunca uno paralelo"): esta suite construye
UNA sola instancia de `SubmitApproval` -- la misma pieza que
`composition/execution_rest.py` usa para el panel -- y la ejercita dos
veces: una llamando `.execute()` directamente (como hace REST) y otra a
traves del `gateway.approve()` de Telegram, sobre dos propuestas gemelas.
Las dos `Authorization` resultantes deben tener la MISMA forma."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from safent_ads.execution.application.undo_execution import UndoExecution
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt
from safent_ads.execution.domain.guardrails import GuardrailEvaluator
from safent_ads.execution.testing.fakes import (
    FakeBrakeStatePort,
    FakeExecutionQueuePort,
    FakeGuardrailSetRepository,
    FakeSpendLedger,
    FakeUnitOfWork,
)
from safent_ads.notifications.application.ports import DecisionKind, UndoResultKind
from safent_ads.notifications.infrastructure.proposal_approval_gateway import (
    ProposalApprovalGateway,
)
from safent_ads.proposals.application.submit_approval import SubmitApproval, SubmitApprovalCommand
from safent_ads.proposals.domain.authorization import AuthorizationChannel, AuthorizationDecision
from safent_ads.proposals.domain.identifiers import AuthorizationId
from safent_ads.proposals.domain.proposal import ProposalState
from safent_ads.proposals.testing.fakes import (
    FakeAuthorizationRepository,
    FakeProposalRepository,
    FakeSignerPort,
)
from safent_ads.shared.clock import FixedClock
from tests.unit.execution.conftest import (
    NOW,
    empty_ledger,
    entity_scope,
    guardrail_set,
    make_pending_proposal,
    make_scheduled_proposal,
)

_SCOPE = entity_scope()


class _NoRowsSession:
    """Doble minimo de `AsyncSession`: sin fila de `ad_entities` que unir
    (el gateway cae al `external_id` de la entidad) ni hermanas de causa
    que listar. Prueba de unidad, no de integracion SQL."""

    async def execute(self, *_args: Any, **_kwargs: Any) -> _EmptyResult:
        return _EmptyResult()


class _EmptyResult:
    def one_or_none(self) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(())


def _build(
    proposals: FakeProposalRepository,
) -> tuple[SubmitApproval, ProposalApprovalGateway, FakeAuthorizationRepository]:
    authorizations = FakeAuthorizationRepository()
    execution_queue = FakeExecutionQueuePort()
    guardrails = FakeGuardrailSetRepository({_SCOPE.ref: guardrail_set()})
    spend_ledger = FakeSpendLedger({_SCOPE.ref: empty_ledger()})
    submit_approval = SubmitApproval(
        proposals=proposals,
        authorizations=authorizations,
        execution_queue=execution_queue,
        brakes=FakeBrakeStatePort(),
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=guardrails,
        spend_ledger=spend_ledger,
        signer=FakeSignerPort(),
        clock=FixedClock(NOW),
    )
    use_cases = _FakeExecutionUseCases(
        proposals=proposals,
        authorizations=authorizations,
        submit_approval=submit_approval,
        guardrail_sets=guardrails,
        undo_execution=None,
    )
    gateway = ProposalApprovalGateway(
        session=_NoRowsSession(), use_cases=use_cases, clock=FixedClock(NOW)  # type: ignore[arg-type]
    )
    return submit_approval, gateway, authorizations


class _FakeExecutionUseCases:
    """Sustituye a `composition.container.ExecutionUseCases`: el gateway
    solo lee `.proposals`, `.submit_approval`, `.guardrail_sets` y
    `.undo_execution` -- el resto de piezas del contenedor real
    (chokepoint, freno, etc.) no le hacen falta."""

    def __init__(
        self, *, proposals, authorizations, submit_approval, guardrail_sets, undo_execution
    ) -> None:
        self.proposals = proposals
        self.authorizations = authorizations
        self.submit_approval = submit_approval
        self.guardrail_sets = guardrail_sets
        self.undo_execution = undo_execution


class TestApproveUsesTheSameSubmitApprovalAsRest:
    async def test_gateway_approve_produces_the_same_kind_of_authorization_as_rest(self) -> None:
        rest_proposal = make_pending_proposal()
        telegram_proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(rest_proposal)
        await proposals.save(telegram_proposal)
        submit_approval, gateway, authorizations = _build(proposals)

        # REST llama a `SubmitApproval.execute()` directamente
        # (`composition/execution_rest.py::approve_proposal`).
        rest_result = await submit_approval.execute(
            SubmitApprovalCommand(
                proposal_id=rest_proposal.proposal_id,
                diff_hash=rest_proposal.diff.diff_hash,
                approved_by="owner@example.com",
                channel=AuthorizationChannel.PANEL,
            )
        )
        # Telegram llama al gateway, que envuelve la MISMA instancia.
        telegram_result = await gateway.approve(
            proposal_id=str(telegram_proposal.proposal_id),
            diff_hash=telegram_proposal.diff.diff_hash,
            decided_by="telegram:111222333",
        )

        assert telegram_result.kind is DecisionKind.APPROVED
        rest_auth = await authorizations.get_active_for_proposal(rest_proposal.proposal_id)
        telegram_auth = await authorizations.get_active_for_proposal(telegram_proposal.proposal_id)
        assert rest_auth is not None
        assert telegram_auth is not None
        assert rest_auth.kind == telegram_auth.kind
        assert rest_auth.decision == telegram_auth.decision == AuthorizationDecision.APPROVED
        assert len(rest_auth.signature) == len(telegram_auth.signature)
        assert rest_auth.channel == AuthorizationChannel.PANEL
        assert telegram_auth.channel == AuthorizationChannel.TELEGRAM
        # Mismo camino de dominio: las dos propuestas terminan SCHEDULED
        # con la misma gracia (ROUTINE), nunca directamente EXECUTED.
        stored_rest = await proposals.get(rest_proposal.proposal_id)
        stored_telegram = await proposals.get(telegram_proposal.proposal_id)
        assert stored_rest is not None and stored_rest.state is ProposalState.SCHEDULED
        assert stored_telegram is not None and stored_telegram.state is ProposalState.SCHEDULED
        assert rest_result.grace_seconds == 20

    async def test_denied_approval_is_reported_never_raised(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        _submit_approval, gateway, _authorizations = _build(proposals)

        result = await gateway.approve(
            proposal_id=str(proposal.proposal_id),
            diff_hash="not-the-live-hash",
            decided_by="telegram:1",
        )

        assert result.kind is DecisionKind.DENIED
        assert result.denial_reason == "DIFF_CHANGED"


class TestRejectAndPostpone:
    async def test_reject_transitions_to_rejected(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        _submit_approval, gateway, _authorizations = _build(proposals)

        result = await gateway.reject(
            proposal_id=str(proposal.proposal_id),
            diff_hash=proposal.diff.diff_hash,
            decided_by="telegram:1",
        )

        assert result.kind.value == "rejected"
        stored = await proposals.get(proposal.proposal_id)
        assert stored is not None and stored.state is ProposalState.REJECTED

    async def test_postpone_sets_postponed_until_in_the_future(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        _submit_approval, gateway, _authorizations = _build(proposals)

        result = await gateway.postpone(
            proposal_id=str(proposal.proposal_id), decided_by="telegram:1", hours=24
        )

        assert result.kind.value == "postponed"
        stored = await proposals.get(proposal.proposal_id)
        assert stored is not None and stored.state is ProposalState.POSTPONED
        assert stored.postpone_until == NOW + timedelta(hours=24)

    async def test_reject_on_already_resolved_proposal_is_denied_not_raised(self) -> None:
        proposal = make_pending_proposal()
        proposal.reject(NOW)
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        _submit_approval, gateway, _authorizations = _build(proposals)

        result = await gateway.reject(
            proposal_id=str(proposal.proposal_id),
            diff_hash=proposal.diff.diff_hash,
            decided_by="telegram:1",
        )

        assert result.kind is DecisionKind.DENIED
        assert result.denial_reason == "PROPOSAL_NOT_PENDING"


class TestGetLiveProposal:
    async def test_missing_proposal_returns_none(self) -> None:
        proposals = FakeProposalRepository()
        _submit_approval, gateway, _authorizations = _build(proposals)

        assert await gateway.get_live_proposal(str(make_pending_proposal().proposal_id)) is None

    async def test_spend_increase_is_flagged_from_before_after(self) -> None:
        proposal = make_pending_proposal()  # budget_diff(): 100 -> 70 (baja)
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        _submit_approval, gateway, _authorizations = _build(proposals)

        view = await gateway.get_live_proposal(str(proposal.proposal_id))

        assert view is not None
        assert view.is_spend_increase is False
        assert view.before_label == "100 €/día"
        assert view.after_label == "70 €/día"


class TestUndoDelegatesToUndoExecution:
    async def test_undo_not_allowed_when_nothing_to_undo(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        submit_approval, _gateway, _authorizations = _build(proposals)
        del submit_approval

        undo_execution = UndoExecution(
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
        use_cases = _FakeExecutionUseCases(
            proposals=proposals,
            authorizations=FakeAuthorizationRepository(),
            submit_approval=None,
            guardrail_sets=FakeGuardrailSetRepository({_SCOPE.ref: guardrail_set()}),
            undo_execution=undo_execution,
        )
        gateway = ProposalApprovalGateway(
            session=_NoRowsSession(), use_cases=use_cases, clock=FixedClock(NOW)  # type: ignore[arg-type]
        )

        result = await gateway.undo(
            proposal_id=str(proposal.proposal_id), initiated_by="telegram:1"
        )

        assert result.kind is UndoResultKind.NOT_ALLOWED

    async def test_undoing_an_already_undone_execution_is_reported_as_already_undone(
        self,
    ) -> None:
        """Bug corregido (esta rama): el "fake messenger" de Telegram
        (`ProposalApprovalGateway`, sin HTTP real) tiene que reportar un
        segundo "Deshacer" distinto de `NOT_ALLOWED` -- es un conflicto
        tipado propio (`ALREADY_UNDONE`), no "nunca hubo nada que deshacer"."""
        proposal = make_scheduled_proposal(grace_period_seconds=0)
        proposal.begin_execution(NOW)
        proposal.record_execution(success=True, now=NOW)
        proposal.pull_events()

        executions = FakeExecutionQueuePort()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        submit_approval, _gateway, _authorizations = _build(proposals)
        del submit_approval

        attempt = ExecutionAttempt.claim(
            proposal.business_id,
            proposal.proposal_id,
            AuthorizationId.new(),
            proposal.diff.diff_hash,
            NOW,
        )
        attempt.start_running()
        attempt.succeed("70.00", "state-after", NOW + timedelta(minutes=30), NOW)
        await executions.save(attempt)

        undo_execution = UndoExecution(
            proposals=proposals,
            authorizations=FakeAuthorizationRepository(),
            executions=executions,
            uow=FakeUnitOfWork(),
            guardrail_evaluator=GuardrailEvaluator(),
            guardrail_sets=FakeGuardrailSetRepository({_SCOPE.ref: guardrail_set()}),
            spend_ledger=FakeSpendLedger({_SCOPE.ref: empty_ledger()}),
            signer=FakeSignerPort(),
            clock=FixedClock(NOW),
        )
        use_cases = _FakeExecutionUseCases(
            proposals=proposals,
            authorizations=FakeAuthorizationRepository(),
            submit_approval=None,
            guardrail_sets=FakeGuardrailSetRepository({_SCOPE.ref: guardrail_set()}),
            undo_execution=undo_execution,
        )
        gateway = ProposalApprovalGateway(
            session=_NoRowsSession(), use_cases=use_cases, clock=FixedClock(NOW)  # type: ignore[arg-type]
        )

        first = await gateway.undo(proposal_id=str(proposal.proposal_id), initiated_by="telegram:1")
        second = await gateway.undo(
            proposal_id=str(proposal.proposal_id), initiated_by="telegram:1"
        )

        assert first.kind is UndoResultKind.RESTORED
        assert second.kind is UndoResultKind.ALREADY_UNDONE
