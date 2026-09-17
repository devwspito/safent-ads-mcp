"""Doble en memoria de `CalendarEventRepository` para los tests de caso de
uso (`catalog/application`, sin Postgres). Implementa el `Protocol`
estructuralmente, sin heredar de el -- mismo criterio que
`FakeSettingsRepository`."""

from __future__ import annotations

from safent_ads.catalog.domain.calendar_event import CalendarEvent, CalendarEventId
from safent_ads.catalog.infrastructure.errors import OfferingNotFoundError
from safent_ads.shared.ids import BusinessId

__all__ = ["FakeCalendarEventRepository"]


class FakeCalendarEventRepository:
    def __init__(self, *, known_offering_ids: frozenset[str] = frozenset()) -> None:
        self._known_offering_ids = known_offering_ids
        self._events: dict[str, CalendarEvent] = {}

    async def create(self, event: CalendarEvent) -> CalendarEvent:
        self._require_offering_exists(event.offering_id)
        self._events[str(event.calendar_event_id)] = event
        return event

    async def update(self, event: CalendarEvent) -> CalendarEvent:
        self._require_offering_exists(event.offering_id)
        self._events[str(event.calendar_event_id)] = event
        return event

    async def delete(self, calendar_event_id: CalendarEventId, business_id: BusinessId) -> bool:
        event = self._events.get(str(calendar_event_id))
        if event is None or event.business_id != business_id:
            return False
        del self._events[str(calendar_event_id)]
        return True

    async def get(
        self, calendar_event_id: CalendarEventId, business_id: BusinessId
    ) -> CalendarEvent | None:
        event = self._events.get(str(calendar_event_id))
        if event is None or event.business_id != business_id:
            return None
        return event

    async def list_for_business(
        self, business_id: BusinessId, *, kind: str | None
    ) -> list[CalendarEvent]:
        return [
            event
            for event in self._events.values()
            if event.business_id == business_id and (kind is None or event.kind.value == kind)
        ]

    def _require_offering_exists(self, offering_id: str | None) -> None:
        if offering_id is not None and offering_id not in self._known_offering_ids:
            raise OfferingNotFoundError(f"offering_id {offering_id} no encontrado")
