"""`DateWindow`: rango inclusivo y helper `trailing` para 3D/7D/14D/30D."""

from __future__ import annotations

from datetime import date

import pytest

from safent_ads.metrics.domain.date_window import DateWindow
from safent_ads.metrics.domain.errors import InvalidDateWindowError


def test_rejects_start_after_end() -> None:
    with pytest.raises(InvalidDateWindowError):
        DateWindow(start_date=date(2026, 9, 9), end_date=date(2026, 9, 8))


def test_trailing_7_days_includes_end_date() -> None:
    window = DateWindow.trailing(end_date=date(2026, 9, 9), days=7)

    assert window.start_date == date(2026, 9, 3)
    assert window.end_date == date(2026, 9, 9)
    assert window.days == 7


def test_contains_is_inclusive_on_both_bounds() -> None:
    window = DateWindow(start_date=date(2026, 9, 1), end_date=date(2026, 9, 7))

    assert window.contains(date(2026, 9, 1)) is True
    assert window.contains(date(2026, 9, 7)) is True
    assert window.contains(date(2026, 8, 31)) is False
    assert window.contains(date(2026, 9, 8)) is False
