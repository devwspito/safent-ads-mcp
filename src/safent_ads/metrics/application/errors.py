"""Errores de aplicacion de `metrics`."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class MixedBatchStatHourError(ApplicationError):
    """Un lote diario trae `stat_hour` o un lote horario lo trae ausente."""


class NoMetricsIngestedError(ApplicationError):
    """No hay ningun `MetricFact` para las entidades pedidas: la frescura no
    se puede calcular todavia."""
