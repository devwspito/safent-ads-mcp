"""Puerto de lectura de `executions` (contracts/rest-api.md §Ejecucion:
`GET /executions?business_id&outcome&since`, `GET /executions/{id}`).
`ExecutionView` es la forma que el panel consume (zod `executionSchema`,
`panel/src/api/schemas/executions.ts`), no la fila cruda de la tabla --
`applied_value`/`previous_value` ya vienen reducidos a escalar
(`Money` pierde la divisa aqui a proposito: el panel la lee de
`estimated_impact`, no del valor aplicado)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from safent_ads.shared.read_models.dto import Money


@dataclass(frozen=True, slots=True)
class ExecutionView:
    execution_id: str
    proposal_id: str
    business_id: str
    entity_name: str
    outcome: str
    applied_value: float | str | None
    previous_value: float | str | None
    estimated_impact: Money
    undo_deadline: datetime | None
    started_at: datetime
    finished_at: datetime | None
    undone_at: datetime | None = None
    compensating_proposal_id: str | None = None
    error_code: str | None = None


class ExecutionReadPort(Protocol):
    async def list_for_business(
        self,
        business_id: str,
        *,
        outcome: str | None,
        since: datetime | None,
        limit: int,
    ) -> list[ExecutionView]: ...

    async def get(self, execution_id: str) -> ExecutionView | None: ...
