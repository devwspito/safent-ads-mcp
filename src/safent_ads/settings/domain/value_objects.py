"""Value objects de `settings` (contracts/rest-api.md §Ajustes: `GET/PUT
/settings`). `timezone`/`currency` los dicta la plataforma (data-model.md
§PlatformAccount) y no viven aqui: se muestran, nunca se validan como
entrada del propietario."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from enum import StrEnum

from safent_ads.shared.errors import DomainError

_HHMM_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DIGEST_HOUR_PATTERN = re.compile(r"^([01]\d|2[0-3]):00$")
_MAX_HOUR = 23


class InvalidActiveHoursError(DomainError):
    """`start`/`end` no respetan `HH:MM`, o `end` no es posterior a `start`."""


class InvalidDigestHourError(DomainError):
    """`digest_hour` no respeta `HH:00` (hora en punto, 00-23)."""


class Theme(StrEnum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class ActiveHours:
    """Ventana horaria activa de un negocio (mismo formato que
    `notifications.domain.value_objects.ActiveHoursWindow`, sin la zona
    horaria -- esa la aporta `businesses.timezone`, ya existente)."""

    start: time
    end: time

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise InvalidActiveHoursError(
                f"active_hours.start ({self.start}) debe ser anterior a "
                f"active_hours.end ({self.end})"
            )

    @classmethod
    def parse(cls, *, start: str, end: str) -> ActiveHours:
        return cls(start=_parse_hhmm(start), end=_parse_hhmm(end))

    @property
    def start_label(self) -> str:
        return self.start.strftime("%H:%M")

    @property
    def end_label(self) -> str:
        return self.end.strftime("%H:%M")


@dataclass(frozen=True, slots=True)
class DigestHour:
    """Hora del digest diario, en punto (0-23). Viaja por el borde HTTP como
    `HH:00` -- mismo formato que `ActiveHours`, para que el panel no mezcle
    una hora suelta con las horas `HH:MM` del resto del formulario."""

    hour: int

    def __post_init__(self) -> None:
        if not 0 <= self.hour <= _MAX_HOUR:
            raise InvalidDigestHourError(f"digest_hour debe estar entre 0 y 23: {self.hour}")

    @classmethod
    def parse(cls, raw: str) -> DigestHour:
        match = _DIGEST_HOUR_PATTERN.match(raw)
        if match is None:
            raise InvalidDigestHourError(f"digest_hour invalido, esperado HH:00: {raw!r}")
        return cls(hour=int(match.group(1)))

    @property
    def label(self) -> str:
        return f"{self.hour:02d}:00"


def _parse_hhmm(raw: str) -> time:
    match = _HHMM_PATTERN.match(raw)
    if match is None:
        raise InvalidActiveHoursError(f"hora invalida, esperado HH:MM: {raw!r}")
    return time(int(match.group(1)), int(match.group(2)))
