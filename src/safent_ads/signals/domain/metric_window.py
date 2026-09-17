"""`MetricWindow` (tasks.md T035, plan.md §4: 'signals -> shared; recibe DTOs,
funciones puras').

Contrato propio de `signals`, deliberadamente desacoplado de
`metrics.domain.MetricWindow`: el grafo de contextos (plan.md §4) declara que
`signals` solo depende de `shared`, nunca de `metrics`. Quien orqueste
`EvaluateEntitySignals` traduce el `MetricWindow` real de `metrics` a este DTO
(capa anticorrupcion)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.domain.errors import NegativeMetricWindowCounterError
from safent_ads.signals.domain.window_span import WindowSpan

_NON_NEGATIVE_FIELDS = (
    "spend_minor",
    "impressions",
    "clicks",
    "reach",
    "conversions",
    "conversion_value_minor",
    "video_views_3s",
    "video_views_75pct",
)


@dataclass(frozen=True, kw_only=True, slots=True)
class MetricWindow:
    entity_ref: EntityRef
    span: WindowSpan
    spend_minor: int
    impressions: int
    clicks: int
    reach: int
    conversions: int
    conversion_value_minor: int
    video_views_3s: int = 0
    video_views_75pct: int = 0
    search_lost_is_budget_pct: float | None = None

    def __post_init__(self) -> None:
        for name in _NON_NEGATIVE_FIELDS:
            if getattr(self, name) < 0:
                raise NegativeMetricWindowCounterError(f"{name} negativo: {getattr(self, name)}")

    @property
    def ctr(self) -> float | None:
        return None if self.impressions == 0 else self.clicks / self.impressions

    @property
    def cpm_minor(self) -> float | None:
        return None if self.impressions == 0 else self.spend_minor / self.impressions * 1000

    @property
    def cpa_minor(self) -> float | None:
        return None if self.conversions == 0 else self.spend_minor / self.conversions

    @property
    def roas(self) -> float | None:
        return None if self.spend_minor == 0 else self.conversion_value_minor / self.spend_minor

    @property
    def frequency(self) -> float | None:
        return None if self.reach == 0 else self.impressions / self.reach

    @property
    def hook_rate(self) -> float | None:
        return None if self.impressions == 0 else self.video_views_3s / self.impressions

    @property
    def hold_rate(self) -> float | None:
        return None if self.video_views_3s == 0 else self.video_views_75pct / self.video_views_3s
