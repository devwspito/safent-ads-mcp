"""DTO de lectura de `settings` (contracts/rest-api.md §Ajustes)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.settings.domain.value_objects import ActiveHours, DigestHour, Theme


@dataclass(frozen=True, slots=True)
class SettingsView:
    business_id: str
    timezone: str
    currency: str
    active_hours: ActiveHours
    digest_hour: DigestHour
    theme: Theme
