"""`RecordDecision` (tasks.md T010): unico punto de entrada al `decision_log`
desde cualquier contexto. Los bounded contexts nunca hablan con el
repositorio directamente (plan.md §4: dependencias en una sola direccion)."""

from __future__ import annotations

from safent_ads.audit.application.ports import DecisionLogRepository
from safent_ads.audit.domain.entry import DecisionLogEntry, PendingDecision


class RecordDecision:
    def __init__(self, repository: DecisionLogRepository) -> None:
        self._repository = repository

    async def execute(self, pending: PendingDecision) -> DecisionLogEntry:
        return await self._repository.append(pending)
