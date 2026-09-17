"""`GetSettings`/`UpdateSettings` (contracts/rest-api.md `GET/PUT
/settings`) sobre `FakeSettingsRepository` -- sin Postgres."""

from __future__ import annotations

import uuid

import pytest

from safent_ads.settings.application.get_settings import (
    BusinessNotFoundError,
    GetSettings,
    GetSettingsCommand,
)
from safent_ads.settings.application.update_settings import UpdateSettings, UpdateSettingsCommand
from safent_ads.settings.domain.value_objects import ActiveHours, DigestHour, Theme
from safent_ads.settings.testing.fakes import FakeSettingsRepository

_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"
_OWNER_ID = uuid.uuid4()
_DEFAULT_ACTIVE_HOURS = ActiveHours.parse(start="08:00", end="21:00")
_DEFAULT_DIGEST_HOUR = DigestHour.parse("09:00")


def _repository(known: dict[str, tuple[str, str]] | None = None) -> FakeSettingsRepository:
    return FakeSettingsRepository(
        known or {_BUSINESS_ID: ("Europe/Madrid", "EUR")},
        default_active_hours=_DEFAULT_ACTIVE_HOURS,
        default_digest_hour=_DEFAULT_DIGEST_HOUR,
    )


class TestGetSettings:
    async def test_returns_the_default_active_hours_and_digest_hour_when_never_customized(
        self,
    ) -> None:
        use_case = GetSettings(_repository())

        view = await use_case.execute(
            GetSettingsCommand(business_id=_BUSINESS_ID, owner_id=_OWNER_ID)
        )

        assert view.timezone == "Europe/Madrid"
        assert view.currency == "EUR"
        assert view.active_hours == _DEFAULT_ACTIVE_HOURS
        assert view.digest_hour == _DEFAULT_DIGEST_HOUR
        assert view.theme is Theme.SYSTEM

    async def test_unknown_business_raises(self) -> None:
        use_case = GetSettings(_repository())

        with pytest.raises(BusinessNotFoundError):
            await use_case.execute(
                GetSettingsCommand(business_id="does-not-exist", owner_id=_OWNER_ID)
            )


class TestUpdateSettings:
    async def test_saves_and_returns_the_new_values(self) -> None:
        repository = _repository()
        use_case = UpdateSettings(repository)
        active_hours = ActiveHours.parse(start="07:00", end="22:00")
        digest_hour = DigestHour.parse("07:00")

        view = await use_case.execute(
            UpdateSettingsCommand(
                business_id=_BUSINESS_ID,
                owner_id=_OWNER_ID,
                active_hours=active_hours,
                digest_hour=digest_hour,
                theme=Theme.DARK,
            )
        )

        assert view.active_hours == active_hours
        assert view.digest_hour == digest_hour
        assert view.theme is Theme.DARK

    async def test_subsequent_get_reflects_the_saved_values(self) -> None:
        repository = _repository()
        active_hours = ActiveHours.parse(start="07:00", end="22:00")
        digest_hour = DigestHour.parse("07:00")
        await UpdateSettings(repository).execute(
            UpdateSettingsCommand(
                business_id=_BUSINESS_ID,
                owner_id=_OWNER_ID,
                active_hours=active_hours,
                digest_hour=digest_hour,
                theme=Theme.LIGHT,
            )
        )

        view = await GetSettings(repository).execute(
            GetSettingsCommand(business_id=_BUSINESS_ID, owner_id=_OWNER_ID)
        )

        assert view.active_hours == active_hours
        assert view.theme is Theme.LIGHT

    async def test_unknown_business_raises(self) -> None:
        use_case = UpdateSettings(_repository())

        with pytest.raises(BusinessNotFoundError):
            await use_case.execute(
                UpdateSettingsCommand(
                    business_id="does-not-exist",
                    owner_id=_OWNER_ID,
                    active_hours=_DEFAULT_ACTIVE_HOURS,
                    digest_hour=_DEFAULT_DIGEST_HOUR,
                    theme=Theme.SYSTEM,
                )
            )
