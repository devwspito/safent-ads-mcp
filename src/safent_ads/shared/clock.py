"""Puerto `Clock` (plan.md N0): el dominio nunca llama a `datetime.now`
directamente para poder probar reglas dependientes del tiempo de forma
determinista."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Puerto: fuente de tiempo actual, siempre timezone-aware en UTC."""

    def now(self) -> datetime: ...


class SystemClock:
    """Implementacion sobre el reloj del sistema operativo."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Reloj fijo para tests: nunca avanza salvo llamada explicita a `advance_to`."""

    def __init__(self, fixed_at: datetime) -> None:
        self._fixed_at = fixed_at

    def now(self) -> datetime:
        return self._fixed_at

    def advance_to(self, moment: datetime) -> None:
        self._fixed_at = moment
