"""Doble en memoria de `SettingsRepository` para los tests de caso de uso
(`settings/application`, sin Postgres). Implementa el `Protocol`
estructuralmente, sin heredar de el -- mismo criterio que
`SqlProposalRepository`."""

from __future__ import annotations

import uuid

from safent_ads.settings.application.dto import SettingsView
from safent_ads.settings.domain.value_objects import ActiveHours, DigestHour, Theme

__all__ = ["FakeSettingsRepository"]


class FakeSettingsRepository:
    """`known_businesses`: `business_id -> (timezone, currency)`, la parte
    que en produccion viene de `businesses` y que este doble nunca escribe
    -- solo la lee para construir la vista."""

    def __init__(
        self,
        known_businesses: dict[str, tuple[str, str]],
        *,
        default_active_hours: ActiveHours,
        default_digest_hour: DigestHour,
    ) -> None:
        self._known_businesses = known_businesses
        self._default_active_hours = default_active_hours
        self._default_digest_hour = default_digest_hour
        self._stored: dict[str, SettingsView] = {}

    async def get(self, business_id: str, owner_id: uuid.UUID) -> SettingsView | None:
        del owner_id
        if business_id in self._stored:
            return self._stored[business_id]
        if business_id not in self._known_businesses:
            return None
        timezone, currency = self._known_businesses[business_id]
        return SettingsView(
            business_id=business_id,
            timezone=timezone,
            currency=currency,
            active_hours=self._default_active_hours,
            digest_hour=self._default_digest_hour,
            theme=Theme.SYSTEM,
        )

    async def save(
        self,
        business_id: str,
        owner_id: uuid.UUID,
        *,
        active_hours: ActiveHours,
        digest_hour: DigestHour,
        theme: Theme,
    ) -> SettingsView | None:
        del owner_id
        if business_id not in self._known_businesses:
            return None
        timezone, currency = self._known_businesses[business_id]
        view = SettingsView(
            business_id=business_id,
            timezone=timezone,
            currency=currency,
            active_hours=active_hours,
            digest_hour=digest_hour,
            theme=theme,
        )
        self._stored[business_id] = view
        return view
