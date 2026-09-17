"""`Restatement` (data-model.md §Restatement, tabla `metrics_restatements`).

Solo-anexable: guarda `old_value`/`new_value` por metrica corregida cuando
plataforma o CRM reportan tarde (FR-5, NFR-7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from safent_ads.shared.ids import EntityRef


@dataclass(frozen=True, kw_only=True, slots=True)
class Restatement:
    entity_ref: EntityRef
    stat_date: date
    stat_hour: int | None
    field_name: str
    old_value: int
    new_value: int
    reason: str
    recorded_at: datetime

    @property
    def is_noop(self) -> bool:
        return self.old_value == self.new_value
