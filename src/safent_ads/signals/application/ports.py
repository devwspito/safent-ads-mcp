"""Puertos de `signals` (plan.md §5, tasks.md T037).

`MetricWindowRepository` sustituye al `MetricFactRepository` sugerido en el
enunciado: `signals` no depende de `metrics` (plan.md §4: 'signals -> shared;
recibe DTOs, funciones puras'), asi que el puerto devuelve el `MetricWindow`
propio de `signals`, no el de `metrics`. Quien implemente el adaptador real
traduce `metrics.domain.MetricWindow` a este DTO (capa anticorrupcion)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.domain.anomaly import Anomaly
from safent_ads.signals.domain.metric_window import MetricWindow
from safent_ads.signals.domain.signal import CreativeSignal, Signal
from safent_ads.signals.domain.window_span import WindowSpan


class MetricWindowRepository(Protocol):
    async def fetch_window(
        self, *, entity_ref: EntityRef, span: WindowSpan, as_of: datetime
    ) -> MetricWindow: ...


class SignalRepository(Protocol):
    async def save(self, signal: Signal) -> None: ...

    async def find_latest_for_entity(self, *, entity_ref: EntityRef) -> Signal | None: ...


class CreativeSignalRepository(Protocol):
    async def save(self, signal: CreativeSignal) -> None: ...

    async def find_latest_for_entity(self, *, entity_ref: EntityRef) -> CreativeSignal | None: ...


class DailySpendSeriesRepository(Protocol):
    """Series diarias crudas para `anomaly.py`: `MetricWindow` agrega, esto
    no — `same_weekday_z` necesita los N puntos individuales del mismo dia
    de la semana."""

    async def fetch_same_weekday_series(
        self, *, entity_ref: EntityRef, as_of: date, weeks: int
    ) -> Sequence[float]: ...

    async def fetch_today_value(self, *, entity_ref: EntityRef, as_of: date) -> float: ...


class AnomalyRepository(Protocol):
    async def save(self, anomaly: Anomaly) -> None: ...
