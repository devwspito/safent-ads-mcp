"""`GpuLease`: token que devuelve `GpuLeasePort.acquire` (creative-port.md
§"Cola GPU"). "Un solo trabajo pesado a la vez... quedarse sin memoria
unificada cuelga la DGX, no mata el proceso" — el lease es la unica prueba
de que el llamador tiene permiso para usar la GPU."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class GpuLease:
    lease_id: uuid.UUID
    acquired_at: datetime
    expires_at: datetime

    def is_expired_at(self, moment: datetime) -> bool:
        return moment >= self.expires_at
