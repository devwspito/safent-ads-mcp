"""`UndoExecution` (T070; FR-15, spec.md pregunta 4). Ventana de gracia para
deshacer una accion autonoma ya ejecutada:

- Dentro de la gracia: si aun no se escribio (`SCHEDULED`), cancela la
  ejecucion programada. Si ya se escribio, restaura el valor anterior de
  inmediato — el clic en "Deshacer" ES la autorizacion humana explicita
  (`human_approval`, nunca `rule_authorization`: revertir puede subir el
  gasto de vuelta al valor original, lo que la autonomia nunca puede hacer
  por si sola — FR-11).
- Fuera de la gracia: crea una propuesta compensatoria `pendiente`, sujeta
  al flujo normal de aprobacion (contracts/telegram.md)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from safent_ads.execution.application.ports import (
    ExecutionQueuePort,
    GuardrailSetRepository,
    UnitOfWork,
)
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, build_idempotency_key
from safent_ads.execution.domain.guardrails import (
    GuardrailChange,
    GuardrailEvaluator,
    GuardrailScope,
    GuardrailVerdict,
    ScopeKind,
    SpendLedger,
    effective_diff,
    money_pair_from_diff,
)
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.domain.undo_policy import UndoGracePolicy
from safent_ads.proposals.application.ports import AuthorizationRepository, ProposalRepository
from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationKind,
    SignerPort,
    sign_authorization,
)
from safent_ads.proposals.domain.cause import Cause
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.proposal import (
    Proposal,
    ProposalState,
    ProposedDiff,
    new_proposal_id,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError

_COMPENSATING_PROPOSAL_TTL = timedelta(hours=24)


class UndoOutcome(StrEnum):
    CANCELLED_SCHEDULED = "cancelled_scheduled"
    RESTORED = "restored"
    COMPENSATING_PROPOSAL_CREATED = "compensating_proposal_created"


class UndoNotAllowedError(ApplicationError):
    """No hay nada que deshacer: la propuesta no existe, nunca se aplico, o
    ya se deshizo antes (solo-anexable — no se puede deshacer dos veces)."""


class ExecutionAlreadyUndoneError(UndoNotAllowedError):
    """El intento de ejecucion original ya tiene `undone_at` -- "Deshacer"
    ya corrio antes para esta propuesta. Subclase de `UndoNotAllowedError`
    para que un catch generico la siga cubriendo, y una mas especifica
    (REST, Telegram) pueda reportar el conflicto tipado (`409
    EXECUTION_ALREADY_UNDONE`) en vez del generico "ventana cerrada"."""


@dataclass(frozen=True, slots=True)
class UndoExecutionCommand:
    proposal_id: ProposalId
    initiated_by: str


@dataclass(frozen=True, slots=True)
class UndoExecutionResult:
    """Lo que `POST /executions/{id}/undo` (contracts/rest-api.md
    §Ejecucion) necesita devolver: el desenlace y, si genero una propuesta
    de restauracion/compensacion, su id -- antes de esto, ningun llamador
    tenia como obtenerlo (gap preexistente, ver informe de esta rama)."""

    outcome: UndoOutcome
    compensating_proposal_id: ProposalId | None = None


class UndoExecution:
    def __init__(
        self,
        *,
        proposals: ProposalRepository,
        authorizations: AuthorizationRepository,
        executions: ExecutionQueuePort,
        uow: UnitOfWork,
        guardrail_evaluator: GuardrailEvaluator,
        guardrail_sets: GuardrailSetRepository,
        spend_ledger: SpendLedger,
        signer: SignerPort,
        clock: Clock,
        undo_policy: UndoGracePolicy | None = None,
    ) -> None:
        self._proposals = proposals
        self._authorizations = authorizations
        self._executions = executions
        self._uow = uow
        self._guardrail_evaluator = guardrail_evaluator
        self._guardrail_sets = guardrail_sets
        self._spend_ledger = spend_ledger
        self._signer = signer
        self._clock = clock
        self._undo_policy = undo_policy or UndoGracePolicy()

    async def execute(self, command: UndoExecutionCommand) -> UndoExecutionResult:
        proposal = await self._proposals.get(command.proposal_id)
        if proposal is None:
            raise UndoNotAllowedError("proposal not found")
        if proposal.diff.managed_binding is not None:
            raise UndoNotAllowedError("managed_human_admission_required")

        if proposal.state is ProposalState.SCHEDULED:
            return await self._cancel_scheduled(proposal)
        if proposal.state is ProposalState.EXECUTED:
            return await self._undo_executed(proposal, command.initiated_by)
        raise UndoNotAllowedError(f"nothing to undo in state {proposal.state}")

    async def _cancel_scheduled(self, proposal: Proposal) -> UndoExecutionResult:
        now = self._clock.now()
        proposal.invalidate("undone_within_grace", now)
        await self._proposals.save(proposal)
        return UndoExecutionResult(outcome=UndoOutcome.CANCELLED_SCHEDULED)

    async def _undo_executed(self, proposal: Proposal, initiated_by: str) -> UndoExecutionResult:
        now = self._clock.now()
        entity_ref = proposal.diff.entity_ref
        # security-review-f4.md B-2: `lock_account` PRIMERO, antes de leer
        # nada -- mismo criterio que `ExecutionChokepoint._pass_gates`. Sin
        # el, un "Deshacer" y un ciclo del chokepoint sobre la MISMA cuenta
        # podrian evaluar guardarrailes/ledger a la vez; con el, se
        # serializan (cuentas distintas siguen en paralelo).
        async with self._uow:
            await self._uow.lock_account(entity_ref)

            attempt = await self._executions.get_for_proposal(proposal.proposal_id)
            if attempt is None or attempt.undo_deadline is None:
                raise UndoNotAllowedError("no execution record with an undo window")

            reverse_diff = _reverse(proposal.diff)
            # El hash de estado ya confirmado por la escritura original
            # (`platform_state_hash_after`) es el que `PlatformStateRevalidator`
            # debe esperar ANTES de deshacer -- si algo mas cambio el estado
            # remoto entre medias, es exactamente la deriva que el paso 5 del
            # chokepoint tiene que detener (plan.md §6).
            expected_state_hash = attempt.platform_state_hash_after
            outcome = (
                UndoOutcome.RESTORED
                if now < attempt.undo_deadline
                else UndoOutcome.COMPENSATING_PROPOSAL_CREATED
            )

            # Idempotencia bajo concurrencia (B-2, CWE-367): escritura
            # CONDICIONAL y ATOMICA -- `UPDATE ... WHERE undone_at IS NULL`
            # -- ANTES de crear nada. Antes de este fix, el chequeo era un
            # `SELECT` (arriba) seguido de un UPSERT incondicional al final:
            # dos "Deshacer" concurrentes leian los dos `undone_at IS NULL`
            # y los dos fabricaban su propia propuesta compensatoria, solo
            # una quedaba referenciada. Con el guard, el segundo pierde AQUI
            # (0 filas) y nunca llega a crear nada.
            claimed = await self._executions.mark_undone_if_pending(
                attempt.execution_id, now=now, reason=outcome.value
            )
            if not claimed:
                raise ExecutionAlreadyUndoneError(
                    f"execution {attempt.execution_id} already undone"
                )

            if outcome is UndoOutcome.RESTORED:
                compensating_proposal_id = await self._restore_immediately(
                    proposal, reverse_diff, initiated_by, now, expected_state_hash
                )
            else:
                compensating_proposal_id = await self._create_compensating_proposal(
                    proposal, reverse_diff, now, expected_state_hash
                )

            # Segundo paso, DELIBERADAMENTE separado del guard de arriba:
            # `executions.compensating_proposal_id` tiene FK a
            # `proposals.id` (0009_executions) y esa fila no existia
            # todavia cuando el guard corrio -- crearla ANTES del guard
            # dejaria una propuesta huerfana si el guard hubiese rechazado
            # el deshacer por duplicado. Sigue en la MISMA transaccion
            # (el candado de `lock_account` no se suelta hasta el
            # commit/rollback de este bloque), asi que esto no reabre
            # ninguna carrera.
            await self._executions.attach_compensating_proposal(
                attempt.execution_id, compensating_proposal_id
            )
        return UndoExecutionResult(
            outcome=outcome, compensating_proposal_id=compensating_proposal_id
        )

    async def _restore_immediately(
        self,
        original: Proposal,
        reverse_diff: ProposedDiff,
        initiated_by: str,
        now: datetime,
        expected_state_hash: str | None,
    ) -> ProposalId:
        restore_proposal = _build_compensating_proposal(
            original, reverse_diff, now, expected_state_hash
        )
        scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(reverse_diff.entity_ref))
        verdict = await self._evaluate_guardrails(scope, reverse_diff)
        # BUG corregido (guardarrailes): firmar el diff EFECTIVO (recortado
        # si el veredicto recorto), nunca `restore_proposal.diff.diff_hash`
        # a ciegas -- ver `effective_diff` (execution/domain/guardrails.py).
        authorization = sign_authorization(
            authorization_id=AuthorizationId.new(),
            proposal_id=restore_proposal.proposal_id,
            kind=AuthorizationKind.HUMAN_APPROVAL,
            proposal_classification=restore_proposal.classification,
            diff_hash=effective_diff(restore_proposal.diff, verdict).diff_hash,
            guardrail_verdict_hash=verdict.verdict_hash,
            issued_by=initiated_by,
            channel=AuthorizationChannel.TELEGRAM,
            decided_at=now,
            expires_at=now + self._undo_policy.default_grace,
            signer=self._signer,
            comment="deshacer dentro de la ventana de gracia",
        )
        restore_proposal.approve(restore_proposal.diff.diff_hash, now)
        restore_proposal.schedule_execution(0, now)
        # BUG corregido: `approvals.proposal_id` tiene FK a `proposals.id` --
        # la propuesta compensatoria debe existir ANTES que su autorizacion,
        # nunca al reves (violaba `approvals_proposal_id_fkey` contra
        # Postgres real).
        await self._proposals.save(restore_proposal)
        await self._authorizations.save(authorization)
        # Gap del carril exec-SQL (handoff): la fila de `executions` de una
        # restauracion no nacia sola -- sin ella, el `ExecutionCycle` no
        # tiene nada que reclamar y "Deshacer" no deshacia de verdad.
        await self._executions.save(
            ExecutionAttempt(
                execution_id=ExecutionId.new(),
                business_id=restore_proposal.business_id,
                proposal_id=restore_proposal.proposal_id,
                authorization_id=authorization.authorization_id,
                idempotency_key=build_idempotency_key(
                    restore_proposal.proposal_id, restore_proposal.diff.diff_hash
                ),
                platform_state_hash_before=expected_state_hash,
            )
        )
        return restore_proposal.proposal_id

    async def _create_compensating_proposal(
        self,
        original: Proposal,
        reverse_diff: ProposedDiff,
        now: datetime,
        expected_state_hash: str | None,
    ) -> ProposalId:
        compensating = _build_compensating_proposal(
            original, reverse_diff, now, expected_state_hash
        )
        await self._proposals.save(compensating)
        return compensating.proposal_id

    async def _evaluate_guardrails(
        self, scope: GuardrailScope, reverse_diff: ProposedDiff
    ) -> GuardrailVerdict:
        guardrails = await self._guardrail_sets.get_effective(scope)
        ledger = await self._spend_ledger.snapshot(scope, reverse_diff.entity_ref)
        before, after = money_pair_from_diff(reverse_diff)
        change = GuardrailChange(
            scope=scope,
            entity_ref=reverse_diff.entity_ref,
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=before,
            after=after,
        )
        return self._guardrail_evaluator.evaluate(change, guardrails, ledger)


def _reverse(diff: ProposedDiff) -> ProposedDiff:
    return ProposedDiff.build(
        entity_ref=diff.entity_ref,
        parameter=diff.parameter,
        before=diff.after,
        after=diff.before,
        managed_binding=diff.managed_binding,
    )


def _build_compensating_proposal(
    original: Proposal,
    reverse_diff: ProposedDiff,
    now: datetime,
    expected_state_hash: str | None,
) -> Proposal:
    return Proposal.raise_proposal(
        proposal_id=new_proposal_id(),
        business_id=original.business_id,
        diff=reverse_diff,
        classification=original.classification,
        cause=_undo_cause(original),
        cause_key=original.cause_key,
        evidence=original.evidence,
        estimated_impact=original.estimated_impact,
        priority=original.priority,
        now=now,
        expires_at=now + _COMPENSATING_PROPOSAL_TTL,
        expected_state_hash=expected_state_hash,
    )


def _undo_cause(original: Proposal) -> Cause:
    return Cause(text=f"Deshacer: {original.cause.text}"[:140], rule_id=original.cause.rule_id)
