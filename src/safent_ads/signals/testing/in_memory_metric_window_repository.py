"""Doble en memoria de `MetricWindowRepository`: devuelve ventanas
precargadas por `(entity_ref, span)`."""

from __future__ import annotations

from datetime import datetime

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.domain.metric_window import MetricWindow
from safent_ads.signals.domain.window_span import WindowSpan


class InMemoryMetricWindowRepository:
    def __init__(self, windows: dict[tuple[EntityRef, WindowSpan], MetricWindow]) -> None:
        self._windows = windows
        self.last_as_of: datetime | None = None

    async def fetch_window(
        self, *, entity_ref: EntityRef, span: WindowSpan, as_of: datetime
    ) -> MetricWindow:
        self.last_as_of = as_of
        return self._windows[(entity_ref, span)]
