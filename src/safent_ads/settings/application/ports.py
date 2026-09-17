"""Puerto de `settings`: `businesses.active_hours_*`/`digest_hour` y
`owner_preferences.theme` se leen/escriben juntos porque `GET/PUT
/settings` los expone como una unica forma (contracts/rest-api.md
§Ajustes) -- separarlos en dos puertos no tiene otro consumidor que lo
justifique todavia."""

from __future__ import annotations

import uuid
from typing import Protocol

from safent_ads.settings.application.dto import SettingsView
from safent_ads.settings.domain.value_objects import ActiveHours, DigestHour, Theme


class SettingsRepository(Protocol):
    async def get(self, business_id: str, owner_id: uuid.UUID) -> SettingsView | None:
        """`None` si `business_id` no existe -- el llamador ya comprobo el
        alcance del propietario (`require_business_access`) antes de
        llegar aqui; esto es solo la red de seguridad de una carrera entre
        la comprobacion y la lectura."""
        ...

    async def save(
        self,
        business_id: str,
        owner_id: uuid.UUID,
        *,
        active_hours: ActiveHours,
        digest_hour: DigestHour,
        theme: Theme,
    ) -> SettingsView | None: ...
