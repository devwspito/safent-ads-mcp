"""`Freshness` (data-model.md §Freshness, tabla `data_freshness`).

Una fila por cuenta de plataforma y nivel de entidad; `is_stale` se deriva de
`lag_minutes > threshold`, nunca se almacena (NFR-1). `platform_account_ref`
es el identificador externo de la cuenta: `accounts` (N1) posee el agregado
real, `metrics` (N2) solo referencia su clave opaca."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safent_ads.shared.ids import EntityLevel


@dataclass(frozen=True, kw_only=True, slots=True)
class Freshness:
    platform_account_ref: str
    entity_level: EntityLevel
    last_ingested_at: datetime

    def lag_minutes(self, *, now: datetime) -> int:
        return int((now - self.last_ingested_at).total_seconds() // 60)

    def is_stale(self, *, now: datetime, threshold_minutes: int) -> bool:
        return self.lag_minutes(now=now) > threshold_minutes
