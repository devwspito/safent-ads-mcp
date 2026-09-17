"""`window_bounds` (mcp/infrastructure/window_bounds.py): traduce el
`Window` del contrato MCP a un rango de fechas SQL. Puro, sin DB -- se
prueba aparte de los puertos SQL que lo consumen."""

from __future__ import annotations

from datetime import date

import pytest

from safent_ads.mcp.application.dto import Window, WindowPreset
from safent_ads.mcp.infrastructure.window_bounds import window_bounds

_TODAY = date(2026, 3, 15)


@pytest.mark.parametrize(
    ("preset", "expected_start"),
    [
        (WindowPreset.TODAY, date(2026, 3, 15)),
        (WindowPreset.THREE_DAYS, date(2026, 3, 13)),
        (WindowPreset.SEVEN_DAYS, date(2026, 3, 9)),
        (WindowPreset.FOURTEEN_DAYS, date(2026, 3, 2)),
        (WindowPreset.THIRTY_DAYS, date(2026, 2, 14)),
    ],
)
def test_preset_window_includes_today_and_lag_zero(
    preset: WindowPreset, expected_start: date
) -> None:
    window = Window(preset=preset, lag_days=0, date_from=None, date_to=None)

    start, end = window_bounds(window, _TODAY)

    assert end == _TODAY
    assert start == expected_start


def test_month_to_date_starts_on_the_first_of_the_month() -> None:
    window = Window(preset=WindowPreset.MONTH_TO_DATE, lag_days=0, date_from=None, date_to=None)

    start, end = window_bounds(window, _TODAY)

    assert start == date(2026, 3, 1)
    assert end == _TODAY


def test_lag_days_shifts_the_whole_window_back() -> None:
    window = Window(preset=WindowPreset.SEVEN_DAYS, lag_days=1, date_from=None, date_to=None)

    start, end = window_bounds(window, _TODAY)

    assert end == date(2026, 3, 14)
    assert start == date(2026, 3, 8)


def test_explicit_range_is_returned_as_is_when_no_preset() -> None:
    window = Window(
        preset=None, lag_days=0, date_from=date(2026, 1, 1), date_to=date(2026, 1, 31)
    )

    start, end = window_bounds(window, _TODAY)

    assert (start, end) == (date(2026, 1, 1), date(2026, 1, 31))
