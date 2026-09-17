"""`ActiveHours`/`DigestHour`/`Theme` (contracts/rest-api.md §Ajustes)."""

from __future__ import annotations

from datetime import time

import pytest

from safent_ads.settings.domain.value_objects import (
    ActiveHours,
    DigestHour,
    InvalidActiveHoursError,
    InvalidDigestHourError,
    Theme,
)


class TestActiveHours:
    def test_parses_valid_hh_mm_range(self) -> None:
        active_hours = ActiveHours.parse(start="08:00", end="21:00")

        assert active_hours.start == time(8, 0)
        assert active_hours.end == time(21, 0)
        assert active_hours.start_label == "08:00"
        assert active_hours.end_label == "21:00"

    def test_rejects_end_not_after_start(self) -> None:
        with pytest.raises(InvalidActiveHoursError):
            ActiveHours.parse(start="21:00", end="08:00")

    def test_rejects_equal_start_and_end(self) -> None:
        with pytest.raises(InvalidActiveHoursError):
            ActiveHours.parse(start="09:00", end="09:00")

    @pytest.mark.parametrize("raw", ["9:00", "09:60", "24:00", "not-a-time", ""])
    def test_rejects_malformed_time(self, raw: str) -> None:
        with pytest.raises(InvalidActiveHoursError):
            ActiveHours.parse(start=raw, end="21:00")


class TestDigestHour:
    def test_parses_hh_00(self) -> None:
        digest_hour = DigestHour.parse("09:00")

        assert digest_hour.hour == 9
        assert digest_hour.label == "09:00"

    def test_accepts_boundary_hours(self) -> None:
        assert DigestHour.parse("00:00").hour == 0
        assert DigestHour.parse("23:00").hour == 23

    @pytest.mark.parametrize("raw", ["24:00", "09:30", "9:00", "not-a-time", ""])
    def test_rejects_invalid_format(self, raw: str) -> None:
        with pytest.raises(InvalidDigestHourError):
            DigestHour.parse(raw)

    def test_construction_out_of_range_raises(self) -> None:
        with pytest.raises(InvalidDigestHourError):
            DigestHour(hour=24)


class TestTheme:
    def test_accepts_the_three_contract_values(self) -> None:
        assert {theme.value for theme in Theme} == {"light", "dark", "system"}
