"""`list_calendar_events_for_business` (contracts/rest-api.md §Calendario,
`GET /calendar-events`): `open_only` se deriva contra la hora inyectada,
`is_window_open` nunca se guarda."""

from __future__ import annotations

from datetime import date

from safent_ads.catalog.application.create_calendar_event import (
    CreateCalendarEvent,
    CreateCalendarEventCommand,
)
from safent_ads.catalog.application.list_calendar_events import list_calendar_events_for_business
from safent_ads.catalog.domain.calendar_event import CalendarEventKind
from safent_ads.catalog.testing.fakes import FakeCalendarEventRepository
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_TODAY = date(2026, 10, 15)


async def _seed(
    repository: FakeCalendarEventRepository,
    *,
    window_start: date,
    window_end: date,
    kind: CalendarEventKind = CalendarEventKind.LAUNCH,
) -> None:
    await CreateCalendarEvent(repository).execute(
        CreateCalendarEventCommand(
            business_id=_BUSINESS_ID,
            name="Evento",
            kind=kind,
            window_start=window_start,
            window_end=window_end,
        )
    )


async def test_open_only_excludes_closed_windows() -> None:
    repository = FakeCalendarEventRepository()
    await _seed(repository, window_start=date(2026, 10, 1), window_end=date(2026, 10, 31))
    await _seed(repository, window_start=date(2020, 1, 1), window_end=date(2020, 2, 1))

    events = await list_calendar_events_for_business(
        repository, _BUSINESS_ID, kind=None, open_only=True, today=_TODAY
    )

    assert len(events) == 1
    assert events[0].is_window_open(_TODAY)


async def test_without_open_only_returns_every_event() -> None:
    repository = FakeCalendarEventRepository()
    await _seed(repository, window_start=date(2026, 10, 1), window_end=date(2026, 10, 31))
    await _seed(repository, window_start=date(2020, 1, 1), window_end=date(2020, 2, 1))

    events = await list_calendar_events_for_business(
        repository, _BUSINESS_ID, kind=None, open_only=False, today=_TODAY
    )

    assert len(events) == 2


async def test_kind_filters_at_the_repository() -> None:
    repository = FakeCalendarEventRepository()
    await _seed(
        repository,
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 31),
        kind=CalendarEventKind.LAUNCH,
    )
    await _seed(
        repository,
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 31),
        kind=CalendarEventKind.PROMOTION,
    )

    events = await list_calendar_events_for_business(
        repository, _BUSINESS_ID, kind="promotion", open_only=False, today=_TODAY
    )

    assert len(events) == 1
    assert events[0].kind is CalendarEventKind.PROMOTION
