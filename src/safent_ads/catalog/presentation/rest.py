"""Borde HTTP de `catalog` (contracts/rest-api.md §Calendario, vocabulary.md
§4): `GET/POST /calendar-events`, `PUT/DELETE /calendar-events/{id}`.

`RequireBusinessAccess` es dependencia FastAPI en cada ruta, tambien en
`PUT`/`DELETE` (mismo criterio que `settings.presentation.rest`): id ajeno
-> 404 `NOT_FOUND`, nunca 403, para no filtrar existencia entre negocios."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.catalog.application.create_calendar_event import (
    CreateCalendarEvent,
    CreateCalendarEventCommand,
)
from safent_ads.catalog.application.errors import CalendarEventNotFoundError
from safent_ads.catalog.application.list_calendar_events import list_calendar_events_for_business
from safent_ads.catalog.application.update_calendar_event import (
    UpdateCalendarEvent,
    UpdateCalendarEventCommand,
)
from safent_ads.catalog.domain.calendar_event import (
    CalendarEvent,
    CalendarEventId,
    CalendarEventKind,
)
from safent_ads.catalog.domain.errors import (
    InvalidCalendarEventNameError,
    InvalidCalendarEventWindowError,
    InvalidEventDateError,
    InvalidRegionError,
)
from safent_ads.catalog.infrastructure.errors import OfferingNotFoundError
from safent_ads.catalog.infrastructure.sql_calendar_event_repository import (
    SqlCalendarEventRepository,
)
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import require_business_access
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = ["build_catalog_router"]

_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]
_BusinessIdDep = Annotated[str, Depends(require_business_access)]
_NOT_FOUND = ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")
_DOMAIN_VALIDATION_ERRORS = (
    InvalidCalendarEventNameError,
    InvalidCalendarEventWindowError,
    InvalidEventDateError,
    InvalidRegionError,
)


def build_catalog_router(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["catalog"])

    @router.get("/calendar-events")
    async def list_calendar_events(
        business_id: _BusinessIdDep, open_only: bool = False, kind: str | None = None
    ) -> dict[str, Any]:
        async with session_factory() as session:
            events = await list_calendar_events_for_business(
                SqlCalendarEventRepository(session),
                BusinessId.parse(business_id),
                kind=kind,
                open_only=open_only,
                today=clock.now().date(),
            )
        return {"items": [_event_to_json(event, today=clock.now().date()) for event in events]}

    @router.post("/calendar-events", status_code=201)
    async def create_calendar_event(
        business_id: _BusinessIdDep, _owner: _OwnerDep, body: Annotated[dict[str, Any], Body(...)]
    ) -> dict[str, Any]:
        command = _parse_create_command(body, business_id=business_id)
        async with session_factory() as session:
            event = await _create(SqlCalendarEventRepository(session), command)
            await session.commit()
        return _event_to_json(event, today=clock.now().date())

    @router.put("/calendar-events/{calendar_event_id}")
    async def update_calendar_event(
        calendar_event_id: str,
        business_id: _BusinessIdDep,
        _owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        command = _parse_update_command(
            body, calendar_event_id=calendar_event_id, business_id=business_id
        )
        async with session_factory() as session:
            event = await _update(SqlCalendarEventRepository(session), command)
            await session.commit()
        return _event_to_json(event, today=clock.now().date())

    @router.delete("/calendar-events/{calendar_event_id}", status_code=204)
    async def delete_calendar_event(
        calendar_event_id: str, business_id: _BusinessIdDep, _owner: _OwnerDep
    ) -> None:
        async with session_factory() as session:
            repository = SqlCalendarEventRepository(session)
            deleted = await repository.delete(
                CalendarEventId.parse(calendar_event_id), BusinessId.parse(business_id)
            )
            if not deleted:
                raise _NOT_FOUND
            await session.commit()

    return router


async def _create(
    repository: SqlCalendarEventRepository, command: CreateCalendarEventCommand
) -> CalendarEvent:
    try:
        return await CreateCalendarEvent(repository).execute(command)
    except _DOMAIN_VALIDATION_ERRORS as exc:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc
    except OfferingNotFoundError as exc:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc


async def _update(
    repository: SqlCalendarEventRepository, command: UpdateCalendarEventCommand
) -> CalendarEvent:
    try:
        return await UpdateCalendarEvent(repository).execute(command)
    except CalendarEventNotFoundError as exc:
        raise _NOT_FOUND from exc
    except _DOMAIN_VALIDATION_ERRORS as exc:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc
    except OfferingNotFoundError as exc:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc


def _parse_create_command(body: dict[str, Any], *, business_id: str) -> CreateCalendarEventCommand:
    fields = _parse_event_fields(body)
    return CreateCalendarEventCommand(business_id=BusinessId.parse(business_id), **fields)


def _parse_update_command(
    body: dict[str, Any], *, calendar_event_id: str, business_id: str
) -> UpdateCalendarEventCommand:
    fields = _parse_event_fields(body)
    return UpdateCalendarEventCommand(
        calendar_event_id=CalendarEventId.parse(calendar_event_id),
        business_id=BusinessId.parse(business_id),
        **fields,
    )


def _parse_event_fields(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _require_str(body, "name"),
        "kind": _parse_kind(body),
        "window_start": _require_date(body, "window_start"),
        "window_end": _require_date(body, "window_end"),
        "offering_id": _optional_str(body, "offering_id"),
        "region": _optional_str(body, "region"),
        "event_date": _optional_date(body, "event_date"),
    }


def _parse_kind(body: dict[str, Any]) -> CalendarEventKind:
    raw = _require_str(body, "kind")
    try:
        return CalendarEventKind(raw)
    except ValueError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message=f"kind invalido: {raw!r}"
        ) from exc


def _require_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=f"{key} requerido")
    return value


def _optional_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=f"{key} invalido")
    return value


def _require_date(body: dict[str, Any], key: str) -> date:
    raw = _require_str(body, key)
    return _parse_date(raw, key)


def _optional_date(body: dict[str, Any], key: str) -> date | None:
    raw = _optional_str(body, key)
    return None if raw is None else _parse_date(raw, key)


def _parse_date(raw: str, key: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message=f"{key} debe ser YYYY-MM-DD"
        ) from exc


def _event_to_json(event: CalendarEvent, *, today: date) -> dict[str, Any]:
    return {
        "calendar_event_id": str(event.calendar_event_id),
        "business_id": str(event.business_id),
        "offering_id": event.offering_id,
        "name": event.name,
        "kind": event.kind.value,
        "region": event.region,
        "window_start": event.window_start.isoformat(),
        "window_end": event.window_end.isoformat(),
        "event_date": event.event_date.isoformat() if event.event_date else None,
        "is_window_open": event.is_window_open(today),
    }
