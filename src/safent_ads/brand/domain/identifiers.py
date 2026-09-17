"""`BrandKitId` (data-model.md no lo define todavia: tool-surface.md §6
anade la biblioteca de marca sobre el modelo existente); un id opaco por
kit, mismo patron que `catalog.domain.calendar_event.CalendarEventId`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BrandKitId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> BrandKitId:
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, raw: str) -> BrandKitId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)
