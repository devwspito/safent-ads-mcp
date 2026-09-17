"""`SearchDecisionLog` (tasks.md T010): lectura paginada del `decision_log`
para el panel (`GET /decision-log`, contracts/rest-api.md)."""

from __future__ import annotations

from safent_ads.audit.application.ports import (
    DecisionLogFilter,
    DecisionLogPage,
    DecisionLogRepository,
)


class SearchDecisionLog:
    def __init__(self, repository: DecisionLogRepository) -> None:
        self._repository = repository

    async def execute(self, criteria: DecisionLogFilter) -> DecisionLogPage:
        return await self._repository.search(criteria)
