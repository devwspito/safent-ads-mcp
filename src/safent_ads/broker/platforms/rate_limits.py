"""Primitivas de auto-limitacion por adaptador (research/ads-platforms-and-
mcps.md §3: "Google Explorer = 2.880 ops/dia"; "Meta Limited: ~20
escrituras/5 min"). El limite lo respeta el adaptador, no el llamante
(contracts/platform-port.md: "Limites de la plataforma respetados en el
adaptador, no en el llamante")."""

from __future__ import annotations

from collections import deque
from datetime import timedelta

from safent_ads.shared.clock import Clock


class DailyOperationBudget:
    """Contador diario de operaciones (Google Ads Explorer: 2.880/dia). Se
    reinicia al cambiar el dia calendario del `Clock` inyectado."""

    def __init__(self, max_operations_per_day: int, clock: Clock) -> None:
        self._max_operations_per_day = max_operations_per_day
        self._clock = clock
        self._count = 0
        self._day = clock.now().date()

    def try_consume(self, operations: int = 1) -> bool:
        self._reset_if_new_day()
        if self._count + operations > self._max_operations_per_day:
            return False
        self._count += operations
        return True

    @property
    def is_exhausted(self) -> bool:
        self._reset_if_new_day()
        return self._count >= self._max_operations_per_day

    def _reset_if_new_day(self) -> None:
        today = self._clock.now().date()
        if today != self._day:
            self._day = today
            self._count = 0


class WriteBudgetWindow:
    """Ventana deslizante de escrituras (Meta Limited: ~20/5 min). Rechaza
    **antes** de intentar la llamada (contracts/platform-port.md punto de
    los adaptadores)."""

    def __init__(self, max_calls: int, window: timedelta, clock: Clock) -> None:
        self._max_calls = max_calls
        self._window = window
        self._clock = clock
        self._call_timestamps: deque[float] = deque()

    def try_consume(self) -> bool:
        now = self._clock.now().timestamp()
        self._evict_expired(now)
        if len(self._call_timestamps) >= self._max_calls:
            return False
        self._call_timestamps.append(now)
        return True

    def _evict_expired(self, now: float) -> None:
        cutoff = now - self._window.total_seconds()
        while self._call_timestamps and self._call_timestamps[0] <= cutoff:
            self._call_timestamps.popleft()
