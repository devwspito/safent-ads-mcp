"""Bloques estadisticos puros, sin `scipy`/`numpy` (regla de la capa de
dominio: solo lenguaje y libreria estandar). `optimization` los reutiliza
para el calculo de muestra (profitability-engine.md §4) y el `optimization`
depende de `economics` en el grafo de contextos (plan.md §4), asi que este
es el lugar correcto y unico donde viven."""

from __future__ import annotations

import math

_ACKLAM_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_ACKLAM_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_ACKLAM_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_ACKLAM_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
)
_LOW_BREAKPOINT = 0.02425
_HIGH_BREAKPOINT = 1 - _LOW_BREAKPOINT


def inverse_normal_cdf(probability: float) -> float:
    """Cuantil de la normal estandar (algoritmo racional de Acklam, error
    relativo < 1.15e-9). Necesario para `z_(1-alpha/2)` y `z_(1-beta)` en el
    calculo de muestra (profitability-engine.md §4) sin depender de `scipy`."""
    if not (0 < probability < 1):
        raise ValueError(f"probability fuera de (0,1): {probability}")
    if probability < _LOW_BREAKPOINT:
        q = math.sqrt(-2 * math.log(probability))
        return _polynomial(_ACKLAM_C, q) / (_polynomial(_ACKLAM_D, q, leading_one=True))
    if probability <= _HIGH_BREAKPOINT:
        q = probability - 0.5
        r = q * q
        return (_polynomial(_ACKLAM_A, r) * q) / _polynomial(_ACKLAM_B, r, leading_one=True)
    q = math.sqrt(-2 * math.log(1 - probability))
    return -_polynomial(_ACKLAM_C, q) / (_polynomial(_ACKLAM_D, q, leading_one=True))


def _polynomial(coeffs: tuple[float, ...], x: float, *, leading_one: bool = False) -> float:
    """Evalua el polinomio de Horner `coeffs[0]*x^n + ... + coeffs[n-1]*x + k`,
    con `k = 1` (denominadores de Acklam) o `k = coeffs[-1]` (numeradores)."""
    result = 0.0
    for c in coeffs:
        result = result * x + c
    return result * x + 1.0 if leading_one else result


def poisson_confidence_interval(count: int, confidence: float) -> tuple[float, float]:
    """IC exacto aproximado de un conteo Poisson (aproximacion de Byar /
    Rothman: error tipico < 0.5% frente al IC exacto de Garwood para
    `count >= 1`, la referencia estandar en epidemiologia cuando no hay
    `scipy.stats.chi2` disponible en la capa de dominio).

    `count = 0` usa el limite superior exacto `-ln(alpha/2)` (caso limite de
    Garwood cuando no hay eventos observados) y limite inferior 0."""
    if count < 0:
        raise ValueError(f"count negativo: {count}")
    if not (0 < confidence < 1):
        raise ValueError(f"confidence fuera de (0,1): {confidence}")
    alpha = 1 - confidence
    z = inverse_normal_cdf(1 - alpha / 2)
    if count == 0:
        return 0.0, -math.log(alpha / 2)
    lower = count * (1 - 1 / (9 * count) - z / (3 * math.sqrt(count))) ** 3
    upper = (count + 1) * (1 - 1 / (9 * (count + 1)) + z / (3 * math.sqrt(count + 1))) ** 3
    return max(lower, 0.0), upper


def sample_size_per_arm(
    *, baseline_rate: float, relative_mde: float, alpha: float = 0.10, power: float = 0.80
) -> int:
    """Tamano de muestra por brazo para detectar `relative_mde` sobre
    `baseline_rate` (profitability-engine.md §4):
    `n = 2*(z_(1-alpha/2)+z_(1-beta))^2 * p(1-p) / delta^2`, `delta` = MDE
    absoluto = `relative_mde * baseline_rate`."""
    if not (0 < baseline_rate < 1):
        raise ValueError(f"baseline_rate fuera de (0,1): {baseline_rate}")
    if relative_mde <= 0:
        raise ValueError(f"relative_mde debe ser > 0: {relative_mde}")
    z_alpha = inverse_normal_cdf(1 - alpha / 2)
    z_power = inverse_normal_cdf(power)
    delta = relative_mde * baseline_rate
    n = 2 * (z_alpha + z_power) ** 2 * baseline_rate * (1 - baseline_rate) / (delta**2)
    return math.ceil(n)
