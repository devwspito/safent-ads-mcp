"""`ResponseCurve` (profitability-engine.md §7): la misma curva de §3b,
Hill de PyMC-Marketing cuando hay histórico, mínimos cuadrados ponderados
sin él. Pura: sin `numpy`/`scipy` (regla de dominio), sin ajuste bayesiano
-- eso vive tras `ResponseCurveFitterPort` en `application/`."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from safent_ads.optimization.domain.errors import (
    OutOfSupportForecastError,
    ResponseCurveNotConvergedError,
)

OUT_OF_SUPPORT_EXTRAPOLATION_FACTOR = 0.50
DEFAULT_FORECAST_BAND_CONFIDENCE = 0.80
_MIN_POWER_EXPONENT = 0.0
_MAX_POWER_EXPONENT = 1.0


class CurveConfidence(StrEnum):
    """profitability-engine.md §7: 'la curva declara curve_confidence:
    experimental|observational' -- se ponderan x2 los cambios deliberados
    del `decision_log`, la curva declara de donde viene."""

    EXPERIMENTAL = "experimental"
    OBSERVATIONAL = "observational"


@dataclass(frozen=True, kw_only=True, slots=True)
class HillCurve:
    """`E(S) = E_max*S/(S+k)`, `dE/dS = E_max*k/(S+k)^2`."""

    e_max: float
    k: float

    def __post_init__(self) -> None:
        if self.e_max <= 0 or self.k <= 0:
            raise ResponseCurveNotConvergedError(
                f"parametros Hill invalidos: e_max={self.e_max}, k={self.k}"
            )

    def business_conversions(self, spend: float) -> float:
        return self.e_max * spend / (spend + self.k)

    def marginal_business_conversions_per_spend(self, spend: float) -> float:
        return self.e_max * self.k / (spend + self.k) ** 2


@dataclass(frozen=True, kw_only=True, slots=True)
class PowerCurve:
    """`E(S) = a*S^b`, `0<b<1`, `dE/dS = b*E/S`."""

    a: float
    b: float

    def __post_init__(self) -> None:
        if self.a <= 0 or not (_MIN_POWER_EXPONENT < self.b < _MAX_POWER_EXPONENT):
            raise ResponseCurveNotConvergedError(
                f"parametros potencia invalidos: a={self.a}, b={self.b}"
            )

    def business_conversions(self, spend: float) -> float:
        return self.a * float(spend**self.b)

    def marginal_business_conversions_per_spend(self, spend: float) -> float:
        if spend <= 0:
            return 0.0
        return self.b * self.business_conversions(spend) / spend


@dataclass(frozen=True, kw_only=True, slots=True)
class ObservedSpendRange:
    """Rango de gasto diario observado en los 90 dias ponderados
    (profitability-engine.md §7): la extrapolacion se limita a +-50% de
    este rango."""

    min_spend: float
    max_spend: float

    def __post_init__(self) -> None:
        if self.min_spend < 0 or self.max_spend < self.min_spend:
            raise ValueError(f"rango invalido: [{self.min_spend}, {self.max_spend}]")

    def is_within_support(self, spend: float) -> bool:
        span = self.max_spend - self.min_spend
        margin = span * OUT_OF_SUPPORT_EXTRAPOLATION_FACTOR
        return (self.min_spend - margin) <= spend <= (self.max_spend + margin)


@dataclass(frozen=True, kw_only=True, slots=True)
class ForecastBand:
    """Prevision de conversiones de negocio a un gasto dado con banda de incertidumbre
    (§7: 'banda del 80%', posterior del MMM o bootstrap de residuos)."""

    spend: float
    expected_business_conversions: float
    low_business_conversions: float
    high_business_conversions: float
    confidence: float
    curve_confidence: CurveConfidence


def forecast(
    curve: HillCurve | PowerCurve,
    *,
    spend: float,
    observed_range: ObservedSpendRange,
    residual_std: float,
    curve_confidence: CurveConfidence,
    band_confidence: float = DEFAULT_FORECAST_BAND_CONFIDENCE,
    z_score: float = 1.2816,
) -> ForecastBand:
    """`z_score` por defecto es el cuantil de una banda del 80% (evita
    depender de `economics.domain.statistics.inverse_normal_cdf` para un
    caso de uso de una sola cola, doble cara -- se puede inyectar el exacto
    si `band_confidence` cambia)."""
    if not observed_range.is_within_support(spend):
        raise OutOfSupportForecastError(
            f"spend={spend} fuera de +-{OUT_OF_SUPPORT_EXTRAPOLATION_FACTOR:.0%} "
            f"del rango observado [{observed_range.min_spend}, {observed_range.max_spend}]"
        )
    expected = curve.business_conversions(spend)
    margin = z_score * residual_std
    return ForecastBand(
        spend=spend,
        expected_business_conversions=expected,
        low_business_conversions=max(expected - margin, 0.0),
        high_business_conversions=expected + margin,
        confidence=band_confidence,
        curve_confidence=curve_confidence,
    )


def scenario_contribution_delta(
    curve: HillCurve | PowerCurve,
    *,
    current_spend: float,
    spend_multiplier: float,
    contribution_margin_per_conversion: float,
) -> float:
    """Escenario "+X%" (§7): `dCM = CM*[E(m*S) - E(S)] - (m-1)*S`."""
    new_spend = current_spend * spend_multiplier
    delta_business_conversions = curve.business_conversions(new_spend) - curve.business_conversions(
        current_spend
    )
    extra_spend = new_spend - current_spend
    return contribution_margin_per_conversion * delta_business_conversions - extra_spend


@dataclass(frozen=True, kw_only=True, slots=True)
class ContributionDeltaBand:
    """Banda de contribucion del escenario "+X%", derivada de `ForecastBand`
    en el gasto propuesto (`simulate_spend_change`, §7/§8: 'delta de
    contribucion, IC, out_of_support')."""

    proposed_spend: float
    expected: float
    low: float
    high: float
    band_confidence: float
    curve_confidence: CurveConfidence


def forecast_contribution_delta_band(
    curve: HillCurve | PowerCurve,
    *,
    current_spend: float,
    spend_multiplier: float,
    contribution_margin_per_conversion: float,
    observed_range: ObservedSpendRange,
    residual_std: float,
    curve_confidence: CurveConfidence,
    band_confidence: float = DEFAULT_FORECAST_BAND_CONFIDENCE,
) -> ContributionDeltaBand:
    """Combina `forecast()` en el gasto propuesto con el punto en el gasto
    actual (conocido operacionalmente, sin banda) para dar una banda de
    `dCM`. Propaga `OutOfSupportForecastError` sin capturarla: la decide
    quien orquesta (§7: 'fuera, out_of_support y no hay numero')."""
    new_spend = current_spend * spend_multiplier
    band = forecast(
        curve,
        spend=new_spend,
        observed_range=observed_range,
        residual_std=residual_std,
        curve_confidence=curve_confidence,
        band_confidence=band_confidence,
    )
    current_business_conversions = curve.business_conversions(current_spend)
    extra_spend = new_spend - current_spend
    cm = contribution_margin_per_conversion
    return ContributionDeltaBand(
        proposed_spend=new_spend,
        expected=(
            cm * (band.expected_business_conversions - current_business_conversions) - extra_spend
        ),
        low=cm * (band.low_business_conversions - current_business_conversions) - extra_spend,
        high=cm * (band.high_business_conversions - current_business_conversions) - extra_spend,
        band_confidence=band.confidence,
        curve_confidence=curve_confidence,
    )
