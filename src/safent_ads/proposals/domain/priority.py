"""`Urgency`, `Priority` y `ExpiryPolicy` (data-model.md: `Urgency`,
`ExpiresAt`; FR-19 lentes urgencia/calendario; spec.md pregunta abierta 5:
"24 h urgencia alta, 72 h resto").

`Urgency` tiene tres niveles (alineado con la migracion `0008_proposals` de
la rama de BD: valores exactos `critical|recommended|minor`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum


class Urgency(StrEnum):
    CRITICAL = "critical"
    RECOMMENDED = "recommended"
    MINOR = "minor"


@dataclass(frozen=True, slots=True)
class Priority:
    """Lentes de ordenacion de una propuesta (FR-19): urgencia y, si aplica,
    el hito de calendario que la motiva."""

    urgency: Urgency
    calendar_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExpiryPolicy:
    """Caducidad por defecto (spec.md pregunta 5): 24 h si `Urgency.CRITICAL`
    ("urgencia alta"), 72 h el resto. Nunca se ejecuta una propuesta caducada
    (invariante 3)."""

    urgent_ttl: timedelta = field(default=timedelta(hours=24))
    normal_ttl: timedelta = field(default=timedelta(hours=72))

    def expires_at(self, urgency: Urgency, now: datetime) -> datetime:
        ttl = self.urgent_ttl if urgency is Urgency.CRITICAL else self.normal_ttl
        return now + ttl
