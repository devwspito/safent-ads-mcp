"""Puerto de `POST /executions/{id}/undo` (contracts/rest-api.md
§Ejecucion): envoltorio de un solo elemento sobre el MISMO `UndoExecution`
que `POST /executions/undo` -- misma ventana de gracia, mismo camino de
decision. Vive en `application/` (no en `execution_rest.py`) para que
`execution/presentation/rest.py` pueda depender de un puerto en vez de
`Container` (plan.md §4); `composition/` aporta la implementacion real
sobre `Container.build_execution_use_cases`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SingleUndoResult:
    ok: bool
    undo_kind: str | None = None
    compensating_proposal_id: str | None = None
    error_code: str | None = None


class SingleExecutionUndoPort(Protocol):
    async def undo(
        self, *, execution_id: str, reason: str, initiated_by: str
    ) -> SingleUndoResult: ...
