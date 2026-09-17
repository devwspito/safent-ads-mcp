"""`MetricFact` (data-model.md §MetricFact, tablas `metrics_daily`/`metrics_hourly`).

Solo contadores crudos por (entity_ref, stat_date[, stat_hour]). Las tasas
(CTR, CPA, ROAS, ...) nunca viven aqui: se derivan en `MetricWindow` como
razon de sumas por ventana."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType

from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.errors import InvalidStatHourError, NegativeCounterError
from safent_ads.shared.ids import EntityRef

_LAST_HOUR_OF_DAY = 23


def _validate_non_negative(name: str, value: int) -> None:
    if value < 0:
        raise NegativeCounterError(f"{name} negativo: {value}")


@dataclass(frozen=True, kw_only=True, slots=True)
class MetricFact:
    """Un hecho crudo ingerido para una entidad en un dia (o una hora de un dia).

    Invariante de identidad natural (FR-5, NFR-6): `(entity_ref, stat_date,
    stat_hour)`; la reingesta es UPSERT sobre esa clave, nunca un INSERT
    duplicado.
    """

    entity_ref: EntityRef
    stat_date: date
    stat_hour: int | None = None
    account_timezone: str
    currency: str
    spend_minor: int
    impressions: int
    clicks: int
    reach: int
    conversions: MappingProxyType[ConversionKind, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    conversion_value_minor: int = 0
    video_views_3s: int = 0
    video_views_75pct: int = 0
    search_lost_is_budget_pct: float | None = None
    search_lost_is_rank_pct: float | None = None
    ingested_at: datetime
    revision: int = 1

    def __post_init__(self) -> None:
        if self.stat_hour is not None and not (0 <= self.stat_hour <= _LAST_HOUR_OF_DAY):
            raise InvalidStatHourError(f"stat_hour fuera de rango: {self.stat_hour}")
        for name, value in (
            ("spend_minor", self.spend_minor),
            ("impressions", self.impressions),
            ("clicks", self.clicks),
            ("reach", self.reach),
            ("conversion_value_minor", self.conversion_value_minor),
            ("video_views_3s", self.video_views_3s),
            ("video_views_75pct", self.video_views_75pct),
        ):
            _validate_non_negative(name, value)
        for kind, count in self.conversions.items():
            _validate_non_negative(f"conversions[{kind}]", count)

    @property
    def natural_key(self) -> tuple[EntityRef, date, int | None]:
        return (self.entity_ref, self.stat_date, self.stat_hour)

    @property
    def is_hourly(self) -> bool:
        return self.stat_hour is not None

    def conversions_of(self, kind: ConversionKind) -> int:
        return self.conversions.get(kind, 0)

    def total_conversions(self) -> int:
        return sum(self.conversions.values())
