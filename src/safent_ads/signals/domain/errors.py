"""Errores de dominio de `signals`."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class SignalStrengthOutOfRangeError(DomainError):
    """`SignalStrength` fuera de [0, 100] (data-model.md §Signal)."""


class NegativeMoneyAtStakeError(DomainError):
    """`MoneyAtStake` con `minor_units` negativo."""


class NegativeMetricWindowCounterError(DomainError):
    """Un contador de `signals.domain.MetricWindow` es negativo."""


class ZeroBaselineError(DomainError):
    """Una formula de anomalia o pacing no puede dividir entre una linea
    base de valor cero."""
