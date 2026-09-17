"""`SingleExecutionUndoPort` (execution.application) sobre
`Container.build_execution_use_cases`: mismo patron que
`composition/mcp_write_adapter.py::ContainerProposalWriteAdapter` --
`execution/presentation/rest.py` no puede importar `Container` (raiz de
composicion, invertiria plan.md §4), asi que el puente vive aqui.

Reusa el MISMO `UndoExecution` (execution.application.undo_execution) que
`POST /executions/undo` en `composition/execution_rest.py::undo_executions`
-- misma ventana de gracia, mismo camino de decision. No importa la
funcion privada `_undo_one` de ese modulo (mismo criterio de la rama: no
crece `execution_rest.py`, ni acopla este adaptador a sus internos)."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.composition.container import Container
from safent_ads.execution.application.single_execution_undo_port import SingleUndoResult
from safent_ads.execution.application.undo_execution import (
    ExecutionAlreadyUndoneError,
    UndoExecutionCommand,
    UndoNotAllowedError,
    UndoOutcome,
)
from safent_ads.proposals.domain.identifiers import ProposalId

__all__ = ["ContainerSingleExecutionUndoAdapter"]

_PROPOSAL_ID_FOR_EXECUTION = text("SELECT proposal_id FROM executions WHERE id = :execution_id")


class ContainerSingleExecutionUndoAdapter:
    def __init__(self, container: Container) -> None:
        self._container = container

    async def undo(self, *, execution_id: str, reason: str, initiated_by: str) -> SingleUndoResult:
        del reason  # `UndoExecutionCommand` no tiene hueco para el motivo
        # (mismo hueco vacio que `POST /executions/undo` ya deja hoy).
        async with self._container.session_factory() as session:
            proposal_id = await _proposal_id_for_execution(session, execution_id)
            if proposal_id is None:
                return SingleUndoResult(ok=False, error_code="NOT_FOUND")
            use_cases = self._container.build_execution_use_cases(session)
            try:
                result = await use_cases.undo_execution.execute(
                    UndoExecutionCommand(proposal_id=proposal_id, initiated_by=initiated_by)
                )
            except ExecutionAlreadyUndoneError:
                return SingleUndoResult(ok=False, error_code="EXECUTION_ALREADY_UNDONE")
            except UndoNotAllowedError as exc:
                return SingleUndoResult(ok=False, error_code=str(exc))
            await session.commit()
        is_cancelled = result.outcome is UndoOutcome.CANCELLED_SCHEDULED
        undo_kind = "cancelled" if is_cancelled else "compensated"
        compensating_proposal_id = (
            str(result.compensating_proposal_id)
            if result.compensating_proposal_id is not None
            else None
        )
        return SingleUndoResult(
            ok=True, undo_kind=undo_kind, compensating_proposal_id=compensating_proposal_id
        )


async def _proposal_id_for_execution(session: AsyncSession, execution_id: str) -> ProposalId | None:
    row = (
        await session.execute(_PROPOSAL_ID_FOR_EXECUTION, {"execution_id": execution_id})
    ).one_or_none()
    return None if row is None else ProposalId(uuid.UUID(str(row.proposal_id)))
