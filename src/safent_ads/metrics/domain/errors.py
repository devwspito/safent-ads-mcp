"""Errores de dominio de `metrics` (data-model.md §MetricFact)."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class NegativeCounterError(DomainError):
    """Un contador crudo (spend, impressions, clicks, reach, conversiones) es negativo."""


class InvalidStatHourError(DomainError):
    """`stat_hour` presente en un hecho diario, o ausente/ fuera de rango en uno horario."""


class InvalidDateWindowError(DomainError):
    """`start_date` posterior a `end_date` en una `DateWindow`."""


class EmptyMetricWindowError(DomainError):
    """Se pidio construir una `MetricWindow` sin ningun `MetricFact` que la respalde."""
