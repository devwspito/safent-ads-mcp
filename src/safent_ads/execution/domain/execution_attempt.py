"""`ExecutionAttempt` — agregado raiz del contexto `execution` (data-model.md
tabla `executions`). Estado `CLAIMED -> RUNNING -> {EXECUTED, FAILED,
UNKNOWN, SKIPPED_DRIFT, BLOCKED_GUARDRAIL, BLOCKED_BRAKE}`.

Invariante central (data-model.md): `idempotency_key` UNIQUE — un reintento
nunca duplica el cambio. Un timeout durante dispatch queda UNKNOWN no terminal;
conserva reserva hasta obtener evidencia concluyente, no autoriza reejecucion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.shared.errors import DomainError
from safent_ads.shared.events import DomainEvent
from safent_ads.shared.ids import BusinessId


class ExecutionAttemptInvariantError(DomainError):
    """Transicion de estado invalida en `ExecutionAttempt`."""


class ExecutionStatus(StrEnum):
    CLAIMED = "claimed"
    RUNNING = "running"
    UNKNOWN = "unknown"
    EXECUTED = "executed"
    FAILED = "failed"
    SKIPPED_DRIFT = "skipped_drift"
    BLOCKED_GUARDRAIL = "blocked_guardrail"
    BLOCKED_BRAKE = "blocked_brake"


_TERMINAL_STATES: frozenset[ExecutionStatus] = frozenset(
    {
        ExecutionStatus.EXECUTED,
        ExecutionStatus.FAILED,
        ExecutionStatus.SKIPPED_DRIFT,
        ExecutionStatus.BLOCKED_GUARDRAIL,
        ExecutionStatus.BLOCKED_BRAKE,
    }
)


def build_idempotency_key(proposal_id: ProposalId, diff_hash: str) -> str:
    """`exec-<proposal_id>-<diff_hash[:12]>` (data-model.md; UNIQUE en
    `executions`). Fija el formato en un unico lugar para que el chokepoint
    y cualquier reintento lo calculen identico."""
    return f"exec-{proposal_id}-{diff_hash[:12]}"


def build_package_step_idempotency_key(publication_id: str, step_index: int) -> str:
    """`pkg-<publication_id>-<step_index>` (`003-paquete-de-campana`
    data-model.md Revision 2 §R2.6, BL-4; contracts/api.md §R2.E).
    Funcion PROPIA, deliberadamente sin reutilizar `build_idempotency_key`:
    prefijo distinto (`pkg-` vs `exec-`), argumentos de otro tipo, para que
    las dos formas sean imposibles de confundir. Estable entre reintentos,
    reanudaciones, re-acunaciones de autorizacion y reconciliaciones —
    nunca depende de `proposal_id` ni de `diff_hash`, que sí pueden cambiar
    entre esas cuatro situaciones."""
    return f"pkg-{publication_id}-{step_index:02d}"


@dataclass(frozen=True, kw_only=True)
class ExecutionAttemptRecorded(DomainEvent):
    """Evento de auditoria del chokepoint: se anexa SIEMPRE, exito o fallo
    (plan.md §6.7), incluso cuando el intento se detiene antes de tocar la
    propuesta (freno, guardarraíl diferido)."""

    execution_id: str
    proposal_id: str
    outcome: str
    error_code: str | None = None


@dataclass(slots=True)
class ExecutionAttempt:
    execution_id: ExecutionId
    business_id: BusinessId
    proposal_id: ProposalId
    authorization_id: AuthorizationId
    idempotency_key: str
    status: ExecutionStatus = ExecutionStatus.CLAIMED
    attempt_count: int = 1
    applied_value: object = None
    previous_value: object = None
    platform_state_hash_before: str | None = None
    platform_state_hash_after: str | None = None
    error_code: str | None = None
    undo_deadline: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    undone_at: datetime | None = None
    undo_reason: str | None = None
    compensating_proposal_id: ProposalId | None = None
    # `003-paquete-de-campana` (BL-4, AL-3): `None` para todo intento que no
    # sea un paso de publicacion -- el ciclo generico (`ExecutionCycle`)
    # nunca reclama una fila con esto relleno (`SqlExecutionQueue.claim_next`
    # sin `proposal_id`).
    package_publication_id: str | None = None
    # El recurso de plataforma que confirma una escritura de CREACION
    # (`WriteResult.created_external_id`) -- `None` en cualquier otra.
    created_external_id: str | None = None

    @classmethod
    def claim(
        cls,
        business_id: BusinessId,
        proposal_id: ProposalId,
        authorization_id: AuthorizationId,
        diff_hash: str,
        now: datetime,
    ) -> ExecutionAttempt:
        return cls(
            execution_id=ExecutionId.new(),
            business_id=business_id,
            proposal_id=proposal_id,
            authorization_id=authorization_id,
            idempotency_key=build_idempotency_key(proposal_id, diff_hash),
            started_at=now,
        )

    @classmethod
    def claim_for_package_step(
        cls,
        *,
        business_id: BusinessId,
        proposal_id: ProposalId,
        authorization_id: AuthorizationId,
        publication_id: str,
        step_index: int,
    ) -> ExecutionAttempt:
        """Como `claim`, pero con la clave de idempotencia PROPIA de un paso
        de paquete (BL-4): `pkg-<publication_id>-<step_index>`, estable
        aunque `chokepoint_step_executor` re-acuñe la `Authorization` al
        reanudar (`build_idempotency_key` habria cambiado con el
        `diff_hash`/`proposal_id`; esta no depende de ninguno).

        H4-1 (revision de codigo): `started_at` se deja en `None`, NUNCA
        `now` -- este metodo INSERTA la fila para que el chokepoint la
        reclame a continuacion (`ChokepointStepExecutor._execute_write_step`
        llama a `execution_queue.save(...)` y LUEGO a `chokepoint.run_once
        (proposal_id=...)`); `claim_next` solo se lleva filas con
        `started_at IS NULL` o con el lease caducado
        (`sql_execution_queue.py::_CLAIM_NEXT`). Con `started_at=now` aqui,
        ese `run_once` inmediato nunca encontraba la fila que el mismo paso
        acababa de crear -- exactamente el patron que `entity_lifecycle_
        actions.py`/`apply_defensive_action.py` ya evitan construyendo el
        `ExecutionAttempt` a mano en vez de con `claim()` (que SI trae
        `started_at`, para representar una fila YA reclamada)."""
        return cls(
            execution_id=ExecutionId.new(),
            business_id=business_id,
            proposal_id=proposal_id,
            authorization_id=authorization_id,
            idempotency_key=build_package_step_idempotency_key(publication_id, step_index),
            package_publication_id=publication_id,
        )

    def audit_event(self, now: datetime) -> ExecutionAttemptRecorded:
        return ExecutionAttemptRecorded(
            business_id=self.business_id,
            occurred_at=now,
            execution_id=str(self.execution_id),
            proposal_id=str(self.proposal_id),
            outcome=self.status.value,
            error_code=self.error_code,
        )

    def start_running(self) -> None:
        self._require_state(ExecutionStatus.CLAIMED)
        self.status = ExecutionStatus.RUNNING

    def succeed(
        self,
        applied_value: object,
        state_hash_after: str,
        undo_deadline: datetime | None,
        now: datetime,
        *,
        created_external_id: str | None = None,
    ) -> None:
        if self.status not in {ExecutionStatus.RUNNING, ExecutionStatus.UNKNOWN}:
            raise ExecutionAttemptInvariantError("only an in-flight attempt can succeed")
        self.status = ExecutionStatus.EXECUTED
        self.applied_value = applied_value
        self.platform_state_hash_after = state_hash_after
        self.error_code = None
        self.undo_deadline = undo_deadline
        self.finished_at = now
        if created_external_id is not None:
            self.created_external_id = created_external_id

    def mark_unknown(self, error_code: str) -> None:
        self._require_not_terminal()
        self.status = ExecutionStatus.UNKNOWN
        self.error_code = error_code
        self.finished_at = None

    def fail(self, error_code: str, now: datetime) -> None:
        self._require_not_terminal()
        self.status = ExecutionStatus.FAILED
        self.error_code = error_code
        self.finished_at = now

    def skip_due_to_drift(self, now: datetime) -> None:
        self._require_not_terminal()
        self.status = ExecutionStatus.SKIPPED_DRIFT
        self.finished_at = now

    def block_by_guardrail(self, reasons: tuple[str, ...], now: datetime) -> None:
        self._require_not_terminal()
        self.status = ExecutionStatus.BLOCKED_GUARDRAIL
        self.error_code = ",".join(reasons)
        self.finished_at = now

    def block_by_brake(self, now: datetime) -> None:
        self._require_not_terminal()
        self.status = ExecutionStatus.BLOCKED_BRAKE
        self.finished_at = now

    def mark_undone(self, now: datetime, compensating_proposal_id: ProposalId, reason: str) -> None:
        """FR-15: deshacer una ejecucion ya escrita anota el intento
        ORIGINAL -- solo-anexable, nunca una segunda vez (INV-1: una sola
        propuesta compensatoria por diff aplicado ya firmado).
        `executions_undo_check` (0009_executions) exige `undo_reason` NOT
        NULL junto a `undone_at`, asi que el dominio nunca deja anotar el
        uno sin el otro."""
        if self.status is not ExecutionStatus.EXECUTED:
            raise ExecutionAttemptInvariantError(
                f"solo se puede deshacer un intento EXECUTED, es {self.status}"
            )
        if self.undone_at is not None:
            raise ExecutionAttemptInvariantError("el intento ya se deshizo antes")
        self.undone_at = now
        self.undo_reason = reason
        self.compensating_proposal_id = compensating_proposal_id

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATES

    def _require_not_terminal(self) -> None:
        if self.is_terminal():
            raise ExecutionAttemptInvariantError(
                f"el intento ya esta en un estado terminal: {self.status}"
            )

    def _require_state(self, expected: ExecutionStatus) -> None:
        if self.status is not expected:
            raise ExecutionAttemptInvariantError(f"se esperaba {expected}, es {self.status}")
