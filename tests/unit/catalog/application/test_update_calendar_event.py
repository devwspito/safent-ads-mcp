"""`UpdateCalendarEvent` (contracts/rest-api.md §Calendario, `PUT
/calendar-events/{id}`) contra `FakeCalendarEventRepository`."""

from __future__ import annotations

from datetime import date

import pytest

from safent_ads.catalog.application.create_calendar_event import (
    CreateCalendarEvent,
    CreateCalendarEventCommand,
)
from safent_ads.catalog.application.errors import CalendarEventNotFoundError
from safent_ads.catalog.application.update_calendar_event import (
    UpdateCalendarEvent,
    UpdateCalendarEventCommand,
)
from safent_ads.catalog.domain.calendar_event import CalendarEventId, CalendarEventKind
from safent_ads.catalog.testing.fakes import FakeCalendarEventRepository
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_OTHER_BUSINESS_ID = BusinessId.new()


async def _seed_event(repository: FakeCalendarEventRepository) -> CalendarEventId:
    created = await CreateCalendarEvent(repository).execute(
        CreateCalendarEventCommand(
            business_id=_BUSINESS_ID,
            name="Lanzamiento otoño",
            kind=CalendarEventKind.LAUNCH,
            window_start=date(2026, 10, 1),
            window_end=date(2026, 10, 31),
        )
    )
    return created.calendar_event_id


async def test_replaces_the_event_fields() -> None:
    repository = FakeCalendarEventRepository()
    calendar_event_id = await _seed_event(repository)

    updated = await UpdateCalendarEvent(repository).execute(
        UpdateCalendarEventCommand(
            calendar_event_id=calendar_event_id,
            business_id=_BUSINESS_ID,
            name="Lanzamiento invierno",
            kind=CalendarEventKind.PROMOTION,
            window_start=date(2026, 12, 1),
            window_end=date(2026, 12, 31),
        )
    )

    assert updated.name == "Lanzamiento invierno"
    assert updated.kind is CalendarEventKind.PROMOTION


async def test_raises_when_the_event_does_not_exist() -> None:
    repository = FakeCalendarEventRepository()

    with pytest.raises(CalendarEventNotFoundError):
        await UpdateCalendarEvent(repository).execute(
            UpdateCalendarEventCommand(
                calendar_event_id=CalendarEventId.new(),
                business_id=_BUSINESS_ID,
                name="No existe",
                kind=CalendarEventKind.LAUNCH,
                window_start=date(2026, 10, 1),
                window_end=date(2026, 10, 31),
            )
        )


async def test_raises_when_the_event_belongs_to_another_business() -> None:
    repository = FakeCalendarEventRepository()
    calendar_event_id = await _seed_event(repository)

    with pytest.raises(CalendarEventNotFoundError):
        await UpdateCalendarEvent(repository).execute(
            UpdateCalendarEventCommand(
                calendar_event_id=calendar_event_id,
                business_id=_OTHER_BUSINESS_ID,
                name="No deberia poder",
                kind=CalendarEventKind.LAUNCH,
                window_start=date(2026, 10, 1),
                window_end=date(2026, 10, 31),
            )
        )
