"""`GetSettings` (contracts/rest-api.md `GET /settings?business_id`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from safent_ads.settings.application.dto import SettingsView
from safent_ads.settings.application.ports import SettingsRepository
from safent_ads.shared.errors import ApplicationError


class BusinessNotFoundError(ApplicationError):
    """`business_id` no existe (o ya no, entre la comprobacion de acceso y
    la lectura -- carrera improbable, nunca ignorada)."""


@dataclass(frozen=True, slots=True)
class GetSettingsCommand:
    business_id: str
    owner_id: uuid.UUID


class GetSettings:
    def __init__(self, repository: SettingsRepository) -> None:
        self._repository = repository

    async def execute(self, command: GetSettingsCommand) -> SettingsView:
        view = await self._repository.get(command.business_id, command.owner_id)
        if view is None:
            raise BusinessNotFoundError(command.business_id)
        return view
