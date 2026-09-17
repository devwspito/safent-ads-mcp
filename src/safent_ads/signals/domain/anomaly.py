"""Formulas de deteccion de anomalias (rule-catalog-and-signals.md §3,
tasks.md T036): `same_weekday_z` (N=8), `ewma_drift` (n=7/28), `wow_delta`.

Puras: reciben series ya preparadas por quien orquesta (mismo dia de la
semana, cronologicas...); no consultan ningun repositorio."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.domain.errors import ZeroBaselineError

_EWMA_FAST_PERIODS = 7
_EWMA_SLOW_PERIODS = 28
_PAGE_Z_THRESHOLD = 3.0
_WARN_Z_THRESHOLD = 2.0


class AnomalyMethod(StrEnum):
    WEEKDAY_Z = "weekday_z"
    EWMA = "ewma"
    WOW = "wow"
    PACE = "pace"


class AnomalySeverity(StrEnum):
    NONE = "none"
    WARN = "warn"
    PAGE = "page"


@dataclass(frozen=True, kw_only=True, slots=True)
class Anomaly:
    """`Anomaly` (data-model.md §Anomaly, tabla `anomalies`): solo notifica,
    nunca genera accion directa."""

    entity_ref: EntityRef
    method: AnomalyMethod
    score: float
    severity: AnomalySeverity
    detected_at: datetime


def classify_z_severity(z: float) -> AnomalySeverity:
    """|z| >= 2 alerta, |z| >= 3 escala a pagina (rule-catalog-and-
    signals.md §3)."""
    magnitude = abs(z)
    if magnitude >= _PAGE_Z_THRESHOLD:
        return AnomalySeverity.PAGE
    if magnitude >= _WARN_Z_THRESHOLD:
        return AnomalySeverity.WARN
    return AnomalySeverity.NONE


def same_weekday_z(x_t: float, same_weekday_values: Sequence[float]) -> float:
    """`z = (x_t - mu_dow) / sigma_dow` sobre las ultimas N observaciones del
    mismo dia de la semana (N=8 tipico; Google usa 26 semanas)."""
    mean = sum(same_weekday_values) / len(same_weekday_values)
    variance = sum((v - mean) ** 2 for v in same_weekday_values) / len(same_weekday_values)
    std_dev = math.sqrt(variance)
    if std_dev == 0:
        return 0.0 if x_t == mean else math.copysign(math.inf, x_t - mean)
    return (x_t - mean) / std_dev


def ewma(values: Sequence[float], periods: int) -> float:
    """`S_t = alpha*x_t + (1-alpha)*S_{t-1}`, `alpha = 2/(periods+1)`, con
    `S_0 = values[0]`."""
    alpha = 2 / (periods + 1)
    smoothed = values[0]
    for value in values[1:]:
        smoothed = alpha * value + (1 - alpha) * smoothed
    return smoothed


def ewma_drift(values: Sequence[float]) -> float:
    """`drift = S7/S28 - 1` sobre la misma serie cronologica (fast n=7,
    slow n=28)."""
    slow = ewma(values, _EWMA_SLOW_PERIODS)
    if slow == 0:
        raise ZeroBaselineError("EWMA lenta (n=28) es cero: drift indefinido")
    fast = ewma(values, _EWMA_FAST_PERIODS)
    return fast / slow - 1


def wow_delta(x_t: float, x_t_minus_7: float) -> float:
    """`delta = x_t / x_(t-7) - 1` (semana contra semana, mismo dia)."""
    if x_t_minus_7 == 0:
        raise ZeroBaselineError("x_t_minus_7 es cero: variacion WoW indefinida")
    return x_t / x_t_minus_7 - 1
