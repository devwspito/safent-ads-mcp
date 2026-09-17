"""`UpdateSettings` (contracts/rest-api.md `PUT /settings
{active_hours, digest_hour, theme}`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from safent_ads.settings.application.dto import SettingsView
from safent_ads.settings.application.get_settings import BusinessNotFoundError
from safent_ads.settings.application.ports import SettingsRepository
from safent_ads.settings.domain.value_objects import ActiveHours, DigestHour, Theme


@dataclass(frozen=True, slots=True)
class UpdateSettingsCommand:
    business_id: str
    owner_id: uuid.UUID
    active_hours: ActiveHours
    digest_hour: DigestHour
    theme: Theme


class UpdateSettings:
    def __init__(self, repository: SettingsRepository) -> None:
        self._repository = repository

    async def execute(self, command: UpdateSettingsCommand) -> SettingsView:
        view = await self._repository.save(
            command.business_id,
            command.owner_id,
            active_hours=command.active_hours,
            digest_hour=command.digest_hour,
            theme=command.theme,
        )
        if view is None:
            raise BusinessNotFoundError(command.business_id)
        return view
