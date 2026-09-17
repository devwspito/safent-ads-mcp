"""Borde HTTP de `execution` (contracts/rest-api.md §Ejecucion, deshacer y
freno): `GET /executions?business_id&outcome&since`, `GET /executions/{id}`,
`POST /executions/{id}/undo`. Depende de dos puertos de `execution.
application` (`ExecutionReadPort`, `SingleExecutionUndoPort`), nunca de
`Container` -- la implementacion real de cada uno la cablea
`composition/app.py` (mismo criterio que `panel.presentation.rest`/
`economics.presentation.rest`)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query

from safent_ads.execution.application.execution_read_port import ExecutionReadPort, ExecutionView
from safent_ads.execution.application.single_execution_undo_port import SingleExecutionUndoPort
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import (
    AuthenticatedCaller,
    CallerDep,
    ensure_business_access,
    require_business_access,
)

__all__ = ["build_execution_read_router"]

_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]
_BusinessIdDep = Annotated[str, Depends(require_business_access)]
_NOT_FOUND = ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")
_MAX_LIST_LIMIT = 200


def build_execution_read_router(
    reads: ExecutionReadPort, undo: SingleExecutionUndoPort
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["execution"])

    @router.get("/executions")
    async def list_executions(
        business_id: _BusinessIdDep,
        outcome: str | None = None,
        since: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=_MAX_LIST_LIMIT)] = _MAX_LIST_LIMIT,
    ) -> dict[str, Any]:
        views = await reads.list_for_business(
            business_id, outcome=outcome, since=since, limit=limit
        )
        return {"items": [_execution_json(view) for view in views]}

    @router.get("/executions/{execution_id}")
    async def get_execution(execution_id: str, caller: CallerDep) -> dict[str, Any]:
        view = await _require_visible_execution(reads, execution_id, caller)
        return _execution_json(view)

    @router.post("/executions/{execution_id}/undo")
    async def undo_execution_route(
        execution_id: str,
        caller: CallerDep,
        owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        reason = _require_str(body, "reason")
        await _require_visible_execution(reads, execution_id, caller)
        result = await undo.undo(execution_id=execution_id, reason=reason, initiated_by=owner.email)
        if not result.ok:
            # Contrato: codigos estables (contracts/rest-api.md §Ejecucion)
            # -- el motivo exacto que devuelve `UndoExecution`
            # (execution.application) es texto libre para depurar, nunca el
            # codigo que ve el cliente, salvo los dos casos tipados de abajo.
            if result.error_code == "NOT_FOUND":
                raise _NOT_FOUND
            if result.error_code == "EXECUTION_ALREADY_UNDONE":
                raise ApiError(
                    status_code=409,
                    code="EXECUTION_ALREADY_UNDONE",
                    message="Esta ejecucion ya se deshizo antes.",
                )
            raise ApiError(
                status_code=409,
                code="UNDO_WINDOW_CLOSED",
                message="No se puede deshacer esta ejecucion.",
            )
        return {
            "undo_kind": result.undo_kind,
            "execution_id": execution_id,
            "compensating_proposal_id": result.compensating_proposal_id,
        }

    return router


async def _require_visible_execution(
    reads: ExecutionReadPort, execution_id: str, caller: AuthenticatedCaller
) -> ExecutionView:
    view = await reads.get(execution_id)
    if view is None:
        raise _NOT_FOUND
    ensure_business_access(view.business_id, caller)
    return view


def _require_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=f"{key} requerido")
    return value


def _execution_json(view: ExecutionView) -> dict[str, Any]:
    return {
        "execution_id": view.execution_id,
        "proposal_id": view.proposal_id,
        "entity_name": view.entity_name,
        "outcome": view.outcome,
        "error_code": view.error_code,
        "applied_value": view.applied_value,
        "previous_value": view.previous_value,
        "estimated_impact": {
            "amount": float(view.estimated_impact.amount),
            "currency": view.estimated_impact.currency,
        },
        "undo_deadline": _optional_isoformat(view.undo_deadline),
        "started_at": view.started_at.isoformat(),
        "finished_at": _optional_isoformat(view.finished_at),
        "undone_at": _optional_isoformat(view.undone_at),
        "compensating_proposal_id": view.compensating_proposal_id,
    }


def _optional_isoformat(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
