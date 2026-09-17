"""`ExecutionReadPort` sobre `executions` (0009_executions) + `proposals`
(estimated_impact) + `ad_entities` (nombre legible) -- las tres tablas que
`ExecutionView` necesita para no obligar al panel a otra llamada por fila
(mismo criterio que `composition/execution_rest.py::_proposal_summary_json`
para `/proposals`).

`outcome='UNDONE'` se deriva de `executions.undone_at`, que `UndoExecution`
(execution.application) anota en la misma unidad de trabajo que crea la
propuesta compensatoria (T070 bugfix) -- el mapeo vive aqui porque es la
lectura correcta del esquema, no en el dominio.

`SqlExecutionReadPort` (una `AsyncSession`, se prueba contra `db_session`)
y `RequestScopedExecutionReadPort` (una sesion por llamada) viven separadas
-- mismo criterio que `panel.infrastructure.sql_read_model.SqlPanelReadPort`/
`RequestScopedPanelReadPort`."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.execution.application.execution_read_port import ExecutionView
from safent_ads.proposals.domain.money import Money as ProposalMoney
from safent_ads.proposals.infrastructure.value_codec import decode_value
from safent_ads.shared.read_models.dto import Money

__all__ = ["RequestScopedExecutionReadPort", "SqlExecutionReadPort"]

_COLUMNS: Final = """
    e.id, e.proposal_id, e.business_id, ae.name AS entity_name,
    e.outcome, e.undone_at, e.error_code,
    e.compensating_proposal_id,
    e.applied_value::text AS applied_value_text, e.previous_value::text AS previous_value_text,
    p.estimated_impact_amount, p.estimated_impact_currency, e.undo_deadline,
    COALESCE(e.started_at, e.scheduled_at) AS started_at, e.finished_at
"""
_FROM: Final = """
    FROM executions e
    JOIN proposals p ON p.id = e.proposal_id
    JOIN ad_entities ae ON ae.business_id = e.business_id AND ae.entity_ref = e.entity_ref
"""

_GET: Final = text(f"SELECT {_COLUMNS} {_FROM} WHERE e.id = :execution_id")

_LIST_TEMPLATE: Final = f"""
    SELECT {_COLUMNS} {_FROM}
    WHERE e.business_id = :business_id
    {{filters}}
    ORDER BY e.created_at DESC
    LIMIT :limit
"""

# `outcome=UNDONE` no es un valor de la columna (ver docstring del modulo):
# filtrarlo significa "ya deshecha", el resto excluye lo ya deshecho para
# no contar una fila dos veces bajo dos filtros distintos.
_UNDONE_FILTER: Final = "AND e.undone_at IS NOT NULL"
_NOT_UNDONE_OUTCOME_FILTER: Final = "AND e.outcome = :outcome AND e.undone_at IS NULL"
_SINCE_FILTER: Final = "AND e.created_at >= :since"


class SqlExecutionReadPort:
    """Implementa `ExecutionReadPort` (execution.application) sobre UNA
    sesion -- solo lectura, sin `commit()` que hacer."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_business(
        self,
        business_id: str,
        *,
        outcome: str | None,
        since: datetime | None,
        limit: int,
    ) -> list[ExecutionView]:
        filters, params = _build_filters(business_id=business_id, outcome=outcome, since=since)
        params["limit"] = limit
        query = text(_LIST_TEMPLATE.format(filters="\n    ".join(filters)))
        rows = (await self._session.execute(query, params)).mappings().all()
        return [_to_view(row) for row in rows]

    async def get(self, execution_id: str) -> ExecutionView | None:
        row = (
            (await self._session.execute(_GET, {"execution_id": execution_id}))
            .mappings()
            .one_or_none()
        )
        return None if row is None else _to_view(row)


class RequestScopedExecutionReadPort:
    """`ExecutionReadPort` real, una sesion por llamada
    (`container.session_factory()`, mismo patron que
    `RequestScopedPanelReadPort`)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_for_business(
        self,
        business_id: str,
        *,
        outcome: str | None,
        since: datetime | None,
        limit: int,
    ) -> list[ExecutionView]:
        async with self._session_factory() as session:
            return await SqlExecutionReadPort(session).list_for_business(
                business_id, outcome=outcome, since=since, limit=limit
            )

    async def get(self, execution_id: str) -> ExecutionView | None:
        async with self._session_factory() as session:
            return await SqlExecutionReadPort(session).get(execution_id)


def _build_filters(
    *, business_id: str, outcome: str | None, since: datetime | None
) -> tuple[list[str], dict[str, Any]]:
    filters: list[str] = []
    params: dict[str, Any] = {"business_id": business_id}
    if outcome == "UNDONE":
        filters.append(_UNDONE_FILTER)
    elif outcome is not None:
        filters.append(_NOT_UNDONE_OUTCOME_FILTER)
        params["outcome"] = outcome
    if since is not None:
        filters.append(_SINCE_FILTER)
        params["since"] = since
    return filters, params


def _to_view(row: RowMapping) -> ExecutionView:
    return ExecutionView(
        execution_id=str(row["id"]),
        proposal_id=str(row["proposal_id"]),
        business_id=str(row["business_id"]),
        entity_name=str(row["entity_name"]),
        outcome="UNDONE" if row["undone_at"] is not None else str(row["outcome"]),
        error_code=row["error_code"],
        applied_value=_scalar(decode_value(row["applied_value_text"])),
        previous_value=_scalar(decode_value(row["previous_value_text"])),
        estimated_impact=Money(
            amount=Decimal(row["estimated_impact_amount"]),
            currency=str(row["estimated_impact_currency"]),
        ),
        undo_deadline=row["undo_deadline"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        undone_at=row["undone_at"],
        compensating_proposal_id=(
            None
            if row["compensating_proposal_id"] is None
            else str(row["compensating_proposal_id"])
        ),
    )


def _scalar(value: object) -> float | str | None:
    if value is None:
        return None
    if isinstance(value, ProposalMoney):
        return float(value.amount)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float | str):
        return value
    return str(value)
