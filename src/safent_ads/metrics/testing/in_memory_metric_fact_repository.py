"""Doble en memoria de `MetricFactRepository`: UPSERT real por clave natural,
para probar idempotencia sin base de datos."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.shared.ids import EntityRef

_NaturalKey = tuple[EntityRef, date, int | None]


class InMemoryMetricFactRepository:
    def __init__(self) -> None:
        self._facts: dict[_NaturalKey, MetricFact] = {}

    async def upsert_many(self, facts: Sequence[MetricFact]) -> None:
        for fact in facts:
            previous = self._facts.get(fact.natural_key)
            if previous is not None and (fact.ingested_at, fact.revision) < (
                previous.ingested_at,
                previous.revision,
            ):
                continue
            self._facts[fact.natural_key] = fact

    async def find_by_natural_key(
        self, *, entity_ref: EntityRef, stat_date: date, stat_hour: int | None
    ) -> MetricFact | None:
        return self._facts.get((entity_ref, stat_date, stat_hour))

    async def find_in_window(
        self, *, entity_ref: EntityRef, start_date: date, end_date: date
    ) -> Sequence[MetricFact]:
        return [
            fact
            for fact in self._facts.values()
            if fact.entity_ref == entity_ref and start_date <= fact.stat_date <= end_date
        ]

    async def latest_ingested_at(self, *, entity_ref: EntityRef) -> datetime | None:
        matching = [f.ingested_at for f in self._facts.values() if f.entity_ref == entity_ref]
        return max(matching, default=None)
