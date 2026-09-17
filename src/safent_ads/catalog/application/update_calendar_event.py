"""`UpdateCalendarEvent` (contracts/rest-api.md §Calendario, `PUT
/calendar-events/{id}`): reemplaza el recurso entero, mismo criterio que
`PUT /settings` -- sin mutacion parcial, el agregado se reconstruye y
revalida entero antes de persistir."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from safent_ads.catalog.application.errors import CalendarEventNotFoundError
from safent_ads.catalog.application.ports import CalendarEventRepository
from safent_ads.catalog.domain.calendar_event import (
    CalendarEvent,
    CalendarEventId,
    CalendarEventKind,
)
from safent_ads.shared.ids import BusinessId

__all__ = ["UpdateCalendarEvent", "UpdateCalendarEventCommand"]


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateCalendarEventCommand:
    calendar_event_id: CalendarEventId
    business_id: BusinessId
    name: str
    kind: CalendarEventKind
    window_start: date
    window_end: date
    offering_id: str | None = None
    region: str | None = None
    event_date: date | None = None


class UpdateCalendarEvent:
    def __init__(self, repository: CalendarEventRepository) -> None:
        self._repository = repository

    async def execute(self, command: UpdateCalendarEventCommand) -> CalendarEvent:
        existing = await self._repository.get(command.calendar_event_id, command.business_id)
        if existing is None:
            raise CalendarEventNotFoundError(str(command.calendar_event_id))
        event = CalendarEvent(
            calendar_event_id=command.calendar_event_id,
            business_id=command.business_id,
            name=command.name,
            kind=command.kind,
            window_start=command.window_start,
            window_end=command.window_end,
            offering_id=command.offering_id,
            region=command.region,
            event_date=command.event_date,
        )
        return await self._repository.update(event)
