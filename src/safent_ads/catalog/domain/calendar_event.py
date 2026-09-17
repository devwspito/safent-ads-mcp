"""`CalendarEvent` — agregado raiz de `catalog` (data-model.md §CalendarEvent,
generalizado por vocabulary.md T175 al lenguaje del producto).

Invariantes que este agregado protege (contracts/rest-api.md §Calendario):
1. `window_start < window_end`: una ventana vacia o invertida no representa
   ningun hito real.
2. `event_date`, cuando existe, no puede caer antes de `window_start`.
3. `name` entre 1 y 120 caracteres.
4. `region`, cuando esta presente, entre 2 y 64 caracteres -- ausente para
   hitos sin alcance geografico (p. ej. una `promotion` de marca).

`is_window_open` se deriva de la fecha actual, nunca se almacena (mismo
criterio que la migracion `0005_catalog_crm` ya documentaba para este
agregado)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from safent_ads.catalog.domain.errors import (
    InvalidCalendarEventNameError,
    InvalidCalendarEventWindowError,
    InvalidEventDateError,
    InvalidRegionError,
)
from safent_ads.shared.ids import BusinessId

_NAME_MIN_LENGTH = 1
_NAME_MAX_LENGTH = 120
_REGION_MIN_LENGTH = 2
_REGION_MAX_LENGTH = 64


@dataclass(frozen=True, slots=True)
class CalendarEventId:
    """Identidad de un `CalendarEvent`. Opaca fuera de `catalog`."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> CalendarEventId:
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, raw: str) -> CalendarEventId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


class CalendarEventKind(StrEnum):
    """`contracts/rest-api.md` §Calendario: hito con ventana, sin
    presuponer ningun vertical."""

    SEASON = "season"
    DEADLINE = "deadline"
    LAUNCH = "launch"
    PROMOTION = "promotion"


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    calendar_event_id: CalendarEventId
    business_id: BusinessId
    name: str
    kind: CalendarEventKind
    window_start: date
    window_end: date
    offering_id: str | None = None
    region: str | None = None
    event_date: date | None = None

    def __post_init__(self) -> None:
        _require_valid_name(self.name)
        _require_valid_region(self.region)
        if self.window_start >= self.window_end:
            raise InvalidCalendarEventWindowError(
                f"window_start ({self.window_start}) debe ser anterior a "
                f"window_end ({self.window_end})"
            )
        if self.event_date is not None and self.event_date < self.window_start:
            raise InvalidEventDateError(
                f"event_date ({self.event_date}) no puede ser anterior a "
                f"window_start ({self.window_start})"
            )

    def is_window_open(self, today: date) -> bool:
        return self.window_start <= today <= self.window_end


def _require_valid_name(name: str) -> None:
    if not (_NAME_MIN_LENGTH <= len(name) <= _NAME_MAX_LENGTH):
        raise InvalidCalendarEventNameError(
            f"name fuera de [{_NAME_MIN_LENGTH}, {_NAME_MAX_LENGTH}]: {len(name)} caracteres"
        )


def _require_valid_region(region: str | None) -> None:
    if region is None:
        return
    if not (_REGION_MIN_LENGTH <= len(region) <= _REGION_MAX_LENGTH):
        raise InvalidRegionError(
            f"region fuera de [{_REGION_MIN_LENGTH}, {_REGION_MAX_LENGTH}]: "
            f"{len(region)} caracteres"
        )
