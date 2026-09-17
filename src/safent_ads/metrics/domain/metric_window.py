"""`MetricWindow` (data-model.md §MetricFact: 'las tasas ... no se almacenan:
se derivan como razon de sumas por ventana').

Agrega una secuencia de `MetricFact` de una misma entidad dentro de una
`DateWindow` y expone las tasas derivadas como propiedades calculadas al
vuelo, nunca persistidas (rule-catalog-and-signals.md §3: 'Rate metrics use
ratio-of-sums per window, never mean-of-ratios')."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.date_window import DateWindow
from safent_ads.metrics.domain.errors import EmptyMetricWindowError
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.shared.ids import EntityRef


@dataclass(frozen=True, kw_only=True, slots=True)
class MetricWindow:
    """Suma de contadores crudos de una entidad sobre una `DateWindow`."""

    entity_ref: EntityRef
    window: DateWindow
    spend_minor: int
    impressions: int
    clicks: int
    reach: int
    conversions: dict[ConversionKind, int]
    conversion_value_minor: int
    video_views_3s: int
    video_views_75pct: int
    fact_count: int

    @classmethod
    def from_facts(
        cls, *, entity_ref: EntityRef, window: DateWindow, facts: Sequence[MetricFact]
    ) -> MetricWindow:
        matched = [
            fact
            for fact in facts
            if fact.entity_ref == entity_ref and window.contains(fact.stat_date)
        ]
        if not matched:
            raise EmptyMetricWindowError(
                f"sin MetricFact para {entity_ref} en {window.start_date}..{window.end_date}"
            )
        conversions: dict[ConversionKind, int] = {}
        for fact in matched:
            for kind, count in fact.conversions.items():
                conversions[kind] = conversions.get(kind, 0) + count
        return cls(
            entity_ref=entity_ref,
            window=window,
            spend_minor=sum(fact.spend_minor for fact in matched),
            impressions=sum(fact.impressions for fact in matched),
            clicks=sum(fact.clicks for fact in matched),
            reach=max((fact.reach for fact in matched), default=0),
            conversions=conversions,
            conversion_value_minor=sum(fact.conversion_value_minor for fact in matched),
            video_views_3s=sum(fact.video_views_3s for fact in matched),
            video_views_75pct=sum(fact.video_views_75pct for fact in matched),
            fact_count=len(matched),
        )

    def conversions_of(self, kind: ConversionKind) -> int:
        return self.conversions.get(kind, 0)

    def total_conversions(self) -> int:
        return sum(self.conversions.values())

    @property
    def ctr(self) -> float | None:
        return None if self.impressions == 0 else self.clicks / self.impressions

    @property
    def cpc_minor(self) -> float | None:
        return None if self.clicks == 0 else self.spend_minor / self.clicks

    @property
    def cpm_minor(self) -> float | None:
        return None if self.impressions == 0 else self.spend_minor / self.impressions * 1000

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

    def cpa_minor(self, kind: ConversionKind) -> float | None:
        count = self.conversions_of(kind)
        return None if count == 0 else self.spend_minor / count

    def cpl_minor(self) -> float | None:
        return self.cpa_minor(ConversionKind.LEAD)
