"""Puerto de escritura/lectura de `catalog` sobre `calendar_events`
(contracts/rest-api.md §Calendario). Protocol, no ABC (plan.md: los puertos
de aplicacion son estructurales, la infraestructura los implementa sin
heredar de nada de `application`)."""

from __future__ import annotations

from typing import Protocol

from safent_ads.catalog.domain.calendar_event import CalendarEvent, CalendarEventId
from safent_ads.shared.ids import BusinessId


class CalendarEventRepository(Protocol):
    async def create(self, event: CalendarEvent) -> CalendarEvent: ...

    async def update(self, event: CalendarEvent) -> CalendarEvent: ...

    async def delete(self, calendar_event_id: CalendarEventId, business_id: BusinessId) -> bool:
        """`True` si existia y se borro; `False` si no habia fila para ese
        `(calendar_event_id, business_id)` -- el borde HTTP decide el 404."""
        ...

    async def get(
        self, calendar_event_id: CalendarEventId, business_id: BusinessId
    ) -> CalendarEvent | None: ...

    async def list_for_business(
        self, business_id: BusinessId, *, kind: str | None
    ) -> list[CalendarEvent]: ...
