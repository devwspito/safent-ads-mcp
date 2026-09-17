"""Dobles en memoria de `DailySpendSeriesRepository` y `AnomalyRepository`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.domain.anomaly import Anomaly


class InMemoryDailySpendSeriesRepository:
    def __init__(self, *, same_weekday_series: Sequence[float], today_value: float) -> None:
        self._same_weekday_series = same_weekday_series
        self._today_value = today_value
        self.last_requested_entity_ref: EntityRef | None = None
        self.last_requested_as_of: date | None = None

    async def fetch_same_weekday_series(
        self, *, entity_ref: EntityRef, as_of: date, weeks: int
    ) -> Sequence[float]:
        self.last_requested_entity_ref = entity_ref
        self.last_requested_as_of = as_of
        return self._same_weekday_series[-weeks:]

    async def fetch_today_value(self, *, entity_ref: EntityRef, as_of: date) -> float:
        self.last_requested_entity_ref = entity_ref
        self.last_requested_as_of = as_of
        return self._today_value


class InMemoryAnomalyRepository:
    def __init__(self) -> None:
        self.saved: list[Anomaly] = []

    async def save(self, anomaly: Anomaly) -> None:
        self.saved.append(anomaly)
