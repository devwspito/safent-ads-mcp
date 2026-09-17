"""Matematica de ritmo/pacing (rule-catalog-and-signals.md §3, tasks.md
T036): `pace_index`, `projected_spend`, `runway_days`, `new_daily_budget`
(par y ponderado), semantica de sobreentrega de Google (2x diario, 30.4x
mensual)."""

from __future__ import annotations

import math

from safent_ads.signals.domain.errors import ZeroBaselineError

GOOGLE_DAILY_OVERDELIVERY_MULTIPLIER = 2.0
GOOGLE_MONTHLY_MULTIPLIER = 30.4


def pace_index(
    *, actual_mtd_minor: int, monthly_cap_minor: int, days_elapsed: int, days_in_month: int
) -> float:
    """`actual_MTD / (monthly_cap * days_elapsed / days_in_month)`. Sano
    95-105%; actuar por encima de 115% o por debajo de una proyeccion 85%."""
    if days_elapsed <= 0:
        raise ZeroBaselineError("days_elapsed debe ser positivo para calcular pace_index")
    expected_spend = monthly_cap_minor * days_elapsed / days_in_month
    if expected_spend == 0:
        raise ZeroBaselineError("gasto esperado es cero: pace_index indefinido")
    return actual_mtd_minor / expected_spend


def projected_spend(*, actual_mtd_minor: int, days_elapsed: int, days_in_month: int) -> float:
    """`actual_MTD / days_elapsed * days_in_month`."""
    if days_elapsed <= 0:
        raise ZeroBaselineError("days_elapsed debe ser positivo para proyectar el gasto")
    return actual_mtd_minor / days_elapsed * days_in_month


def runway_days(*, remaining_minor: int, avg_daily_minor: float) -> float:
    """`remaining / avg_daily`. Sin gasto diario, el presupuesto no se agota
    nunca (runway infinito, no un error)."""
    if avg_daily_minor == 0:
        return math.inf
    return remaining_minor / avg_daily_minor


def new_daily_budget_even(*, remaining_minor: int, days_left: int) -> float:
    """Estrategia "even" de Google Flexible Budgets: `remaining / days_left`."""
    if days_left <= 0:
        raise ZeroBaselineError("days_left debe ser positivo para repartir el presupuesto")
    return remaining_minor / days_left


def new_daily_budget_weighted(*, remaining_minor: int, days_left: int) -> float:
    """Estrategia "weighted": `remaining / (2*days_left - 1)`, adelanta gasto
    a los primeros dias del tramo restante."""
    if days_left <= 0:
        raise ZeroBaselineError("days_left debe ser positivo para repartir el presupuesto")
    return remaining_minor / (2 * days_left - 1)


def google_max_daily_charge_minor(daily_budget_minor: int) -> int:
    """Google puede cobrar hasta 2x el presupuesto diario en un solo dia."""
    return round(daily_budget_minor * GOOGLE_DAILY_OVERDELIVERY_MULTIPLIER)


def google_monthly_equivalent_minor(daily_budget_minor: int) -> int:
    """Equivalente mensual de un presupuesto diario: `daily * 30.4`."""
    return round(daily_budget_minor * GOOGLE_MONTHLY_MULTIPLIER)
