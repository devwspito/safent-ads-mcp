"""`CreateCalendarEvent` (contracts/rest-api.md §Calendario) contra
`FakeCalendarEventRepository`: sin Postgres, valida el ensamblado del
agregado y la propagacion de `OfferingNotFoundError`."""

from __future__ import annotations

from datetime import date

import pytest

from safent_ads.catalog.application.create_calendar_event import (
    CreateCalendarEvent,
    CreateCalendarEventCommand,
)
from safent_ads.catalog.domain.calendar_event import CalendarEventKind
from safent_ads.catalog.domain.errors import InvalidCalendarEventWindowError
from safent_ads.catalog.infrastructure.errors import OfferingNotFoundError
from safent_ads.catalog.testing.fakes import FakeCalendarEventRepository
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()


def _command(**overrides: object) -> CreateCalendarEventCommand:
    fields: dict[str, object] = {
        "business_id": _BUSINESS_ID,
        "name": "Lanzamiento otoño",
        "kind": CalendarEventKind.LAUNCH,
        "window_start": date(2026, 10, 1),
        "window_end": date(2026, 10, 31),
    }
    fields.update(overrides)
    return CreateCalendarEventCommand(**fields)  # type: ignore[arg-type]


async def test_creates_and_persists_the_event() -> None:
    repository = FakeCalendarEventRepository()
    use_case = CreateCalendarEvent(repository)

    event = await use_case.execute(_command())

    stored = await repository.get(event.calendar_event_id, _BUSINESS_ID)
    assert stored is not None
    assert stored.name == "Lanzamiento otoño"


async def test_propagates_invalid_window_from_the_aggregate() -> None:
    repository = FakeCalendarEventRepository()
    use_case = CreateCalendarEvent(repository)

    with pytest.raises(InvalidCalendarEventWindowError):
        await use_case.execute(
            _command(window_start=date(2026, 10, 31), window_end=date(2026, 10, 1))
        )


async def test_rejects_an_unknown_offering_id() -> None:
    repository = FakeCalendarEventRepository(known_offering_ids=frozenset())
    use_case = CreateCalendarEvent(repository)

    with pytest.raises(OfferingNotFoundError):
        await use_case.execute(_command(offering_id="unknown-offering"))


async def test_accepts_a_known_offering_id() -> None:
    repository = FakeCalendarEventRepository(known_offering_ids=frozenset({"offering-1"}))
    use_case = CreateCalendarEvent(repository)

    event = await use_case.execute(_command(offering_id="offering-1"))

    assert event.offering_id == "offering-1"
