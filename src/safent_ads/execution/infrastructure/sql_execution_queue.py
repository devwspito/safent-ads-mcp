"""Adaptador SQL de `ExecutionQueuePort` sobre `executions` (0009_executions).

Paso 1 de plan.md §6: `SELECT ... FOR UPDATE SKIP LOCKED` sobre el indice
parcial `ix_executions_claim (outcome, scheduled_at)`. Dos trabajadores
compitiendo por la cola nunca reciben la misma fila: el que llega segundo la
salta en vez de bloquearse.

Reclamar **marca la fila**, no solo la bloquea: `started_at` pasa de NULL a
la hora del reclamo dentro de la misma transaccion en la que se evalua el
guardarraíl (threat-model.md C-15, `SqlUnitOfWork`). Asi, cuando esa
transaccion confirma, la fila deja de ser reclamable aunque el candado ya no
exista — de otro modo un segundo trabajador la reclamaria mientras el
primero habla con la plataforma. `outcome` se queda en `CLAIMED` a
proposito: el dominio (`ExecutionAttempt.start_running`) es quien decide
cuando pasa a `RUNNING`, y un adaptador no adelanta transiciones de estado.

Una reclamacion huerfana (proceso muerto entre el reclamo y el desenlace) se
recupera sola: pasado `claim_lease` la fila vuelve a ser reclamable y
`attempt_count` sube, que es justo para lo que existe esa columna.

Traducciones de frontera (el dominio nunca las ve):
- `ExecutionStatus.EXECUTED` <-> `outcome = 'SUCCEEDED'`; el resto es el
  mismo nombre en mayusculas.
- `executions.error_code` es NOT NULL para todo estado terminal
  (`executions_failure_has_code_check`), pero el dominio deja sin codigo la
  deriva y el freno: el motivo lo pone esta tabla de traduccion, en un unico
  sitio.
- `entity_ref` y `platform_state_hash_before` son NOT NULL en la tabla y no
  viven en `ExecutionAttempt`: al insertar se toman de la propuesta (su
  `entity_ref` y el `expected_state_hash` de su sobre `evidence`).
- `scheduled_at` nace de `proposals.execution_scheduled_at`, la hora en que
  vence la gracia para deshacer (FR-15): la fila no es reclamable antes, y
  esa espera la impone el WHERE del reclamo, no un temporizador en memoria."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.domain.execution_attempt import ExecutionAttempt, ExecutionStatus
from safent_ads.execution.domain.identifiers import ExecutionId
from safent_ads.execution.infrastructure.errors import (
    DuplicateExecutionError,
    ExecutionRowRejectedError,
    UnknownProposalError,
)
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.infrastructure.value_codec import decode_value, encode_value
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = ["DEFAULT_CLAIM_LEASE", "SqlExecutionQueue"]

# Una reclamacion sin desenlace se considera abandonada pasado este tiempo.
# Holgado frente al timeout del cliente del broker (10 s) para no reclamar
# dos veces una escritura que sigue en vuelo.
DEFAULT_CLAIM_LEASE: Final = timedelta(minutes=5)

_OUTCOME_BY_STATUS: Final[dict[ExecutionStatus, str]] = {
    ExecutionStatus.CLAIMED: "CLAIMED",
    ExecutionStatus.RUNNING: "RUNNING",
    ExecutionStatus.UNKNOWN: "UNKNOWN",
    ExecutionStatus.EXECUTED: "SUCCEEDED",
    ExecutionStatus.FAILED: "FAILED",
    ExecutionStatus.SKIPPED_DRIFT: "SKIPPED_DRIFT",
    ExecutionStatus.BLOCKED_GUARDRAIL: "BLOCKED_GUARDRAIL",
    ExecutionStatus.BLOCKED_BRAKE: "BLOCKED_BRAKE",
}
_STATUS_BY_OUTCOME: Final[dict[str, ExecutionStatus]] = {
    outcome: status for status, outcome in _OUTCOME_BY_STATUS.items()
}

# `executions_failure_has_code_check`: todo desenlace no exitoso lleva
# motivo. El dominio solo pone codigo en `fail()` y en el bloqueo por
# guardarraíl; los otros dos lo llevan implicito en el estado.
_IMPLICIT_ERROR_CODE: Final[dict[ExecutionStatus, str]] = {
    ExecutionStatus.SKIPPED_DRIFT: "platform_state_drifted",
    ExecutionStatus.BLOCKED_BRAKE: "emergency_brake_engaged",
}

_COLUMNS: Final = """
    id, business_id, proposal_id, authorization_id, idempotency_key, outcome, attempt_count,
    previous_value::text AS previous_value_text, applied_value::text AS applied_value_text,
    platform_state_hash_before, platform_state_hash_after, error_code, undo_deadline,
    started_at, finished_at, undone_at, undo_reason, compensating_proposal_id,
    package_publication_id, created_external_id
"""

# Paso 1 de plan.md §6. El WHERE es exactamente el predicado de
# `ix_executions_claim` mas el estado del reclamo, de modo que la busqueda
# recorre el indice parcial y no la tabla.
#
# `003-paquete-de-campana` (AL-3/J-T1): sin `proposal_id`, el ciclo GENERICO
# (`ExecutionCycle`) excluye toda fila con `package_publication_id` --
# `RunPackagePublication` es la UNICA via que avanza un paso de paquete, y
# lo hace pidiendo esa fila por `proposal_id` explicito (que SI la
# encuentra: el filtro de abajo solo se aplica cuando `proposal_id IS
# NULL`, coincide con `ix_executions_claim_excludes_package_steps`).
_CLAIM_NEXT_TEMPLATE: Final = """
    UPDATE executions AS claimed
       SET started_at = :now, attempt_count = claimed.attempt_count + 1
     WHERE claimed.id = (
            SELECT candidate.id
              FROM executions AS candidate
             WHERE candidate.outcome IN ('CLAIMED', 'RUNNING', 'UNKNOWN')
               AND candidate.scheduled_at <= :now
               AND (candidate.started_at IS NULL OR candidate.started_at < :lease_cutoff)
               AND (
                    CAST(:proposal_id AS UUID) IS NULL
                    OR candidate.proposal_id = CAST(:proposal_id AS UUID)
               )
               AND (
                    CAST(:proposal_id AS UUID) IS NOT NULL
                    OR candidate.package_publication_id IS NULL
               )
             ORDER BY candidate.scheduled_at
             FOR UPDATE SKIP LOCKED
             LIMIT 1)
    RETURNING {columns}
"""
_CLAIM_NEXT: Final = _CLAIM_NEXT_TEMPLATE.format(columns=_COLUMNS)

_UPSERT: Final = """
    INSERT INTO executions (
        id, proposal_id, authorization_id, business_id, entity_ref, idempotency_key,
        previous_value, applied_value, platform_state_hash_before, platform_state_hash_after,
        attempt_count, outcome, error_code, scheduled_at, started_at, finished_at, undo_deadline,
        undone_at, undo_reason, compensating_proposal_id, package_publication_id,
        created_external_id
    )
    SELECT :id, :proposal_id, :authorization_id, :business_id, proposal.entity_ref,
           :idempotency_key, CAST(:previous_value AS JSONB), CAST(:applied_value AS JSONB),
           COALESCE(:state_hash_before, proposal.evidence->>'expected_state_hash'),
           :state_hash_after, :attempt_count, :outcome, :error_code,
           COALESCE(proposal.execution_scheduled_at, :now), :started_at,
           :finished_at, :undo_deadline, :undone_at, :undo_reason,
           CAST(:compensating_proposal_id AS UUID), :package_publication_id,
           :created_external_id
      FROM proposals AS proposal
     WHERE proposal.id = :proposal_id
    ON CONFLICT (id) DO UPDATE SET
        previous_value            = EXCLUDED.previous_value,
        applied_value             = EXCLUDED.applied_value,
        platform_state_hash_after = EXCLUDED.platform_state_hash_after,
        attempt_count             = EXCLUDED.attempt_count,
        outcome                   = EXCLUDED.outcome,
        error_code                = EXCLUDED.error_code,
        started_at                = EXCLUDED.started_at,
        finished_at               = EXCLUDED.finished_at,
        undo_deadline             = EXCLUDED.undo_deadline,
        undone_at                 = EXCLUDED.undone_at,
        undo_reason               = EXCLUDED.undo_reason,
        compensating_proposal_id  = EXCLUDED.compensating_proposal_id,
        created_external_id       = EXCLUDED.created_external_id
    RETURNING id
"""

# `ix_executions_proposal` cubre el filtro; el orden desempata dos intentos
# del mismo instante.
_LATEST_FOR_PROPOSAL_TEMPLATE: Final = """
    SELECT {columns} FROM executions
     WHERE proposal_id = :proposal_id
     ORDER BY created_at DESC, id DESC
     LIMIT 1
"""
_LATEST_FOR_PROPOSAL: Final = _LATEST_FOR_PROPOSAL_TEMPLATE.format(columns=_COLUMNS)

# security-review-f4.md B-2: guard atomico de "Deshacer" -- el `WHERE
# undone_at IS NULL` hace que, bajo dos transacciones concurrentes sobre el
# mismo `execution_id`, como mucho una vea `RETURNING id` no vacio. La fila
# la bloquea el propio UPDATE (MVCC de Postgres): la segunda transaccion
# espera a que la primera confirme/deshaga y entonces reevalua el WHERE con
# el valor ya confirmado.
_MARK_UNDONE_IF_PENDING: Final = """
    UPDATE executions
       SET undone_at = :now, undo_reason = :reason
     WHERE id = :execution_id AND undone_at IS NULL
    RETURNING id
"""

# Segundo paso, deliberadamente separado del guard de arriba: la propuesta
# compensatoria (FK de esta columna) no existe todavia cuando el guard
# corre -- ver el docstring de `ExecutionQueuePort.attach_compensating_proposal`.
_ATTACH_COMPENSATING_PROPOSAL: Final = """
    UPDATE executions
       SET compensating_proposal_id = :compensating_proposal_id
     WHERE id = :execution_id
"""


class SqlExecutionQueue:
    """Implementa `execution.application.ports.ExecutionQueuePort`.

    Vive dentro de la transaccion del `AsyncSession` que le pasan y no hace
    `commit()`: el limite lo pone `SqlUnitOfWork`, porque el reclamo y la
    evaluacion del guardarraíl tienen que caber en la MISMA transaccion
    (threat-model.md C-15)."""

    def __init__(
        self,
        session: AsyncSession,
        clock: Clock,
        *,
        claim_lease: timedelta = DEFAULT_CLAIM_LEASE,
    ) -> None:
        self._session = session
        self._clock = clock
        self._claim_lease = claim_lease
        self._current: ExecutionAttempt | None = None

    async def claim_next(self, *, proposal_id: ProposalId | None = None) -> ExecutionAttempt | None:
        now = self._clock.now()
        result = await self._session.execute(
            text(_CLAIM_NEXT),
            {
                "now": now,
                "lease_cutoff": now - self._claim_lease,
                "proposal_id": str(proposal_id) if proposal_id is not None else None,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        self._current = _to_attempt(row)
        return self._current

    async def save(self, attempt: ExecutionAttempt) -> None:
        params = {**_upsert_params(attempt), "now": self._clock.now()}
        try:
            result = await self._session.execute(text(_UPSERT), params)
        except IntegrityError as exc:
            raise _translate(exc, attempt) from exc
        if result.first() is None:
            raise UnknownProposalError(
                f"la propuesta {attempt.proposal_id} no existe: no se puede anotar su ejecucion"
            )
        self._current = attempt

    async def get_for_proposal(self, proposal_id: ProposalId) -> ExecutionAttempt | None:
        result = await self._session.execute(
            text(_LATEST_FOR_PROPOSAL), {"proposal_id": str(proposal_id)}
        )
        row = result.mappings().one_or_none()
        return None if row is None else _to_attempt(row)

    async def mark_undone_if_pending(
        self, execution_id: ExecutionId, *, now: datetime, reason: str
    ) -> bool:
        result = await self._session.execute(
            text(_MARK_UNDONE_IF_PENDING),
            {"execution_id": str(execution_id), "now": now, "reason": reason},
        )
        return result.first() is not None

    async def attach_compensating_proposal(
        self, execution_id: ExecutionId, compensating_proposal_id: ProposalId
    ) -> None:
        await self._session.execute(
            text(_ATTACH_COMPENSATING_PROPOSAL),
            {
                "execution_id": str(execution_id),
                "compensating_proposal_id": str(compensating_proposal_id),
            },
        )

    def current(self) -> ExecutionAttempt | None:
        """Ultimo intento reclamado o guardado por esta cola. `SqlSpendLedger`
        lo consulta para saber a que ejecucion atribuir el apunte de gasto:
        el puerto `SpendLedger` no recibe el intento, y un apunte sin
        ejecucion rompe el tope diario (C-17)."""
        return self._current


def _upsert_params(attempt: ExecutionAttempt) -> dict[str, Any]:
    return {
        "id": str(attempt.execution_id),
        "proposal_id": str(attempt.proposal_id),
        "authorization_id": str(attempt.authorization_id),
        "business_id": str(attempt.business_id),
        "idempotency_key": attempt.idempotency_key,
        # NOT NULL en la tabla: un valor previo desconocido viaja como JSON
        # `null` envuelto, nunca como NULL de SQL.
        "previous_value": encode_value(attempt.previous_value),
        # NULL de verdad mientras no haya escritura confirmada, para que
        # `executions_success_is_verified_check` siga siendo una frontera.
        "applied_value": (
            None if attempt.applied_value is None else encode_value(attempt.applied_value)
        ),
        "state_hash_before": attempt.platform_state_hash_before,
        "state_hash_after": attempt.platform_state_hash_after,
        "attempt_count": attempt.attempt_count,
        "outcome": _OUTCOME_BY_STATUS[attempt.status],
        "error_code": attempt.error_code or _IMPLICIT_ERROR_CODE.get(attempt.status),
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "undo_deadline": attempt.undo_deadline,
        "undone_at": attempt.undone_at,
        "undo_reason": attempt.undo_reason,
        "compensating_proposal_id": (
            str(attempt.compensating_proposal_id)
            if attempt.compensating_proposal_id is not None
            else None
        ),
        "package_publication_id": attempt.package_publication_id,
        "created_external_id": attempt.created_external_id,
    }


def _translate(exc: IntegrityError, attempt: ExecutionAttempt) -> Exception:
    message = str(exc.orig)
    if "executions_idempotency_key_unique" in message:
        return DuplicateExecutionError(
            f"{attempt.idempotency_key} ya tiene ejecucion: el reintento no duplica el cambio"
        )
    return ExecutionRowRejectedError(f"executions rechazo el intento {attempt.execution_id}")


def _to_attempt(row: RowMapping) -> ExecutionAttempt:
    return ExecutionAttempt(
        execution_id=ExecutionId(uuid.UUID(str(row["id"]))),
        business_id=BusinessId.parse(str(row["business_id"])),
        proposal_id=ProposalId(uuid.UUID(str(row["proposal_id"]))),
        authorization_id=AuthorizationId(uuid.UUID(str(row["authorization_id"]))),
        idempotency_key=str(row["idempotency_key"]),
        status=_STATUS_BY_OUTCOME[str(row["outcome"])],
        attempt_count=int(row["attempt_count"]),
        applied_value=decode_value(row["applied_value_text"]),
        previous_value=decode_value(row["previous_value_text"]),
        platform_state_hash_before=_optional_str(row["platform_state_hash_before"]),
        platform_state_hash_after=_optional_str(row["platform_state_hash_after"]),
        error_code=_optional_str(row["error_code"]),
        undo_deadline=_optional_datetime(row["undo_deadline"]),
        started_at=_optional_datetime(row["started_at"]),
        finished_at=_optional_datetime(row["finished_at"]),
        undone_at=_optional_datetime(row["undone_at"]),
        undo_reason=_optional_str(row["undo_reason"]),
        compensating_proposal_id=_optional_proposal_id(row["compensating_proposal_id"]),
        package_publication_id=_optional_str(row["package_publication_id"]),
        created_external_id=_optional_str(row["created_external_id"]),
    )


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_datetime(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _optional_proposal_id(value: object) -> ProposalId | None:
    return None if value is None else ProposalId(uuid.UUID(str(value)))
