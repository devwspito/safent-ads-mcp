"""Ventana de gracia para deshacer una accion autonoma ya ejecutada
(FR-15; spec.md pregunta abierta 4: "30 min bajadas, 2 h pausas").

Distinta de la ventana de gracia de `Proposal.schedule_execution` (esa
retrasa la escritura para permitir cancelar ANTES de ejecutar; esta permite
deshacer DESPUES de ejecutar)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from safent_ads.proposals.domain.proposal import ProposedDiff

_BUDGET_PARAMETER = "daily_budget"
_STATUS_PARAMETER = "status"
_DELETED_VALUE = "DELETED"


@dataclass(frozen=True, slots=True)
class UndoGracePolicy:
    budget_decrease_grace: timedelta = field(default=timedelta(minutes=30))
    pause_grace: timedelta = field(default=timedelta(hours=2))
    default_grace: timedelta = field(default=timedelta(minutes=30))

    def grace_for(self, diff: ProposedDiff) -> timedelta | None:
        """`None` = sin ventana de deshacer -- `ExecutionChokepoint._succeed`
        deja `ExecutionAttempt.undo_deadline` a `None`, y `UndoExecution` ya
        rechaza deshacer un intento sin ventana (su guardia existente
        `attempt.undo_deadline is None`). Borrar una entidad (PauseEntity/
        ResumeEntity/DeleteEntity, design.md §0.7) es
        irreversible en Meta/Google: no hay mutacion inversa que ofrecer."""
        if diff.parameter.startswith(("new_campaign:", "new_ad_set:", "new_ad:")):
            # Creation has no inverse update. Deleting a created resource would
            # require its own separately approved operation, not `after=None`.
            return timedelta(0)
        if diff.parameter == _BUDGET_PARAMETER:
            return self.budget_decrease_grace
        if diff.parameter == _STATUS_PARAMETER:
            if diff.after == _DELETED_VALUE:
                return None
            return self.pause_grace
        return self.default_grace
