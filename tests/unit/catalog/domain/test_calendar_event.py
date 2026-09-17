"""`CalendarEvent` (data-model.md §CalendarEvent, vocabulary.md T175):
invariantes de ventana, `event_date`, longitud de `name`/`region` y
`is_window_open` derivado."""

from __future__ import annotations

from datetime import date

import pytest

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
from safent_ads.shared.ids import BusinessId

_WINDOW_START = date(2026, 10, 1)
_WINDOW_END = date(2026, 10, 31)


def _build(**overrides: object) -> CalendarEvent:
    fields: dict[str, object] = {
        "calendar_event_id": CalendarEventId.new(),
        "business_id": BusinessId.new(),
        "name": "Lanzamiento otoño",
        "kind": CalendarEventKind.LAUNCH,
        "window_start": _WINDOW_START,
        "window_end": _WINDOW_END,
    }
    fields.update(overrides)
    return CalendarEvent(**fields)  # type: ignore[arg-type]


class TestWindowInvariant:
    def test_accepts_a_window_where_start_precedes_end(self) -> None:
        event = _build(window_start=_WINDOW_START, window_end=_WINDOW_END)
        assert event.window_start < event.window_end

    def test_rejects_a_window_where_start_equals_end(self) -> None:
        with pytest.raises(InvalidCalendarEventWindowError):
            _build(window_start=_WINDOW_START, window_end=_WINDOW_START)

    def test_rejects_a_window_where_start_is_after_end(self) -> None:
        with pytest.raises(InvalidCalendarEventWindowError):
            _build(window_start=_WINDOW_END, window_end=_WINDOW_START)


class TestEventDateInvariant:
    def test_accepts_no_event_date(self) -> None:
        event = _build(event_date=None)
        assert event.event_date is None

    def test_accepts_an_event_date_on_or_after_window_start(self) -> None:
        event = _build(event_date=_WINDOW_START)
        assert event.event_date == _WINDOW_START

    def test_rejects_an_event_date_before_window_start(self) -> None:
        with pytest.raises(InvalidEventDateError):
            _build(event_date=date(2026, 9, 30))


class TestNameInvariant:
    def test_rejects_an_empty_name(self) -> None:
        with pytest.raises(InvalidCalendarEventNameError):
            _build(name="")

    def test_rejects_a_name_over_120_characters(self) -> None:
        with pytest.raises(InvalidCalendarEventNameError):
            _build(name="x" * 121)

    def test_accepts_a_name_at_the_120_character_boundary(self) -> None:
        event = _build(name="x" * 120)
        assert len(event.name) == 120


class TestRegionInvariant:
    def test_accepts_no_region(self) -> None:
        event = _build(region=None)
        assert event.region is None

    def test_rejects_a_region_under_2_characters(self) -> None:
        with pytest.raises(InvalidRegionError):
            _build(region="a")

    def test_rejects_a_region_over_64_characters(self) -> None:
        with pytest.raises(InvalidRegionError):
            _build(region="a" * 65)

    def test_accepts_a_region_within_bounds(self) -> None:
        event = _build(region="Madrid")
        assert event.region == "Madrid"


class TestCalendarEventKind:
    @pytest.mark.parametrize("kind", list(CalendarEventKind))
    def test_every_kind_builds_a_valid_event(self, kind: CalendarEventKind) -> None:
        event = _build(kind=kind)
        assert event.kind is kind

    def test_has_exactly_the_four_kinds_from_the_contract(self) -> None:
        assert {kind.value for kind in CalendarEventKind} == {
            "season",
            "deadline",
            "launch",
            "promotion",
        }


class TestIsWindowOpen:
    def test_is_open_within_the_window(self) -> None:
        event = _build()
        assert event.is_window_open(date(2026, 10, 15)) is True

    def test_is_open_on_the_boundaries(self) -> None:
        event = _build()
        assert event.is_window_open(_WINDOW_START) is True
        assert event.is_window_open(_WINDOW_END) is True

    def test_is_closed_before_the_window(self) -> None:
        event = _build()
        assert event.is_window_open(date(2026, 9, 30)) is False

    def test_is_closed_after_the_window(self) -> None:
        event = _build()
        assert event.is_window_open(date(2026, 11, 1)) is False
