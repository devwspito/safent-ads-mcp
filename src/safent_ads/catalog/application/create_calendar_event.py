"""`CreateCalendarEvent` (contracts/rest-api.md §Calendario, `POST
/calendar-events`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from safent_ads.catalog.application.ports import CalendarEventRepository
from safent_ads.catalog.domain.calendar_event import (
    CalendarEvent,
    CalendarEventId,
    CalendarEventKind,
)
from safent_ads.shared.ids import BusinessId

__all__ = ["CreateCalendarEvent", "CreateCalendarEventCommand"]


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateCalendarEventCommand:
    business_id: BusinessId
    name: str
    kind: CalendarEventKind
    window_start: date
    window_end: date
    offering_id: str | None = None
    region: str | None = None
    event_date: date | None = None


class CreateCalendarEvent:
    def __init__(self, repository: CalendarEventRepository) -> None:
        self._repository = repository

    async def execute(self, command: CreateCalendarEventCommand) -> CalendarEvent:
        event = CalendarEvent(
            calendar_event_id=CalendarEventId.new(),
            business_id=command.business_id,
            name=command.name,
            kind=command.kind,
            window_start=command.window_start,
            window_end=command.window_end,
            offering_id=command.offering_id,
            region=command.region,
            event_date=command.event_date,
        )
        return await self._repository.create(event)
