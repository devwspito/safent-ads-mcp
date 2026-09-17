"""Errores de dominio de `economics` (profitability-engine.md §1-§2).

Nombrados por invariante violado, nunca genericos: quien captura sabe por
que capturar sin inspeccionar el mensaje."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class RateOutOfRangeError(DomainError):
    """Una tasa (IVA, descuento, refund, cobro, cvr) no esta en [0,1]."""


class ThetaBelowFloorError(DomainError):
    """`theta` (margen retenido) por debajo del suelo 0,20 (profitability-
    engine.md §1: 'comprar a contribucion cero esta prohibido')."""


class MarginHorizonTooShortError(DomainError):
    """`margin_horizon_days` < `median_lag_days` (profitability-engine.md
    §1: invariante `margin_horizon_days >= median_lag_days`)."""


class NonPositiveTargetCpeError(DomainError):
    """`target_cpe >= net_revenue`: la contribucion objetivo no deja margen
    (profitability-engine.md §1 invariante `target_cpe < net_revenue`)."""


class ProfileVersionOverlapError(DomainError):
    """Dos versiones de `UnitEconomicsProfile` del mismo producto se
    solapan en el tiempo (profitability-engine.md §1: 'versiones sin
    solapamiento')."""


class InsufficientLagObservationsError(DomainError):
    """La curva de rezago no tiene observaciones suficientes para estimar
    `F(d)` con confianza (profitability-engine.md §2: 'n >= 100 leads')."""


class NoConvergedCurveError(DomainError):
    """La curva de rezago nunca converge (`F(D_max) == 0`): no hay ninguna
    conversion observada en la cohorte."""
