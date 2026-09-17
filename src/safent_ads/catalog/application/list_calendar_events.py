"""Lectura de `GET /calendar-events` (contracts/rest-api.md §Calendario):
funcion pura sobre el puerto, mismo criterio que
`rules.application.read_models.rule_catalog_view.list_rule_views` -- sin
clase de caso de uso, la unica logica es filtrar `open_only` con la hora
inyectada (`is_window_open` se deriva, nunca se guarda)."""

from __future__ import annotations

from datetime import date

from safent_ads.catalog.application.ports import CalendarEventRepository
from safent_ads.catalog.domain.calendar_event import CalendarEvent
from safent_ads.shared.ids import BusinessId

__all__ = ["list_calendar_events_for_business"]


async def list_calendar_events_for_business(
    repository: CalendarEventRepository,
    business_id: BusinessId,
    *,
    kind: str | None,
    open_only: bool,
    today: date,
) -> list[CalendarEvent]:
    events = await repository.list_for_business(business_id, kind=kind)
    if not open_only:
        return events
    return [event for event in events if event.is_window_open(today)]
