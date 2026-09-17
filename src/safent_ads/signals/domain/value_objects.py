"""VOs pequenos de `signals` (data-model.md §Signal): fuerza, dinero en
juego y evidencia. Agrupados en un solo modulo porque son cohesivos y cada
uno es de una linea de invariante (convencion de `shared/ids.py`)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.signals.domain.errors import (
    NegativeMoneyAtStakeError,
    SignalStrengthOutOfRangeError,
)
from safent_ads.signals.domain.window_span import WindowSpan

_MAX_STRENGTH = 100
_MIN_STRENGTH = 0


@dataclass(frozen=True, slots=True)
class SignalStrength:
    value: int

    def __post_init__(self) -> None:
        if not (_MIN_STRENGTH <= self.value <= _MAX_STRENGTH):
            raise SignalStrengthOutOfRangeError(f"strength fuera de [0,100]: {self.value}")

    @classmethod
    def clamped(cls, raw: float) -> SignalStrength:
        return cls(round(min(max(raw, _MIN_STRENGTH), _MAX_STRENGTH)))


@dataclass(frozen=True, kw_only=True, slots=True)
class MoneyAtStake:
    minor_units: int
    currency: str

    def __post_init__(self) -> None:
        if self.minor_units < 0:
            raise NegativeMoneyAtStakeError(f"minor_units negativo: {self.minor_units}")


@dataclass(frozen=True, kw_only=True, slots=True)
class Evidence:
    """Metrica comparada contra objetivo o linea base (data-model.md §Signal:
    'Evidence (metricas comparadas contra objetivo)')."""

    metric: str
    actual: float
    target: float | None
    baseline: float | None
    span: WindowSpan
