"""`MarginalEstimate` — estimador tactico de ROAS marginal por ventanas
emparejadas (profitability-engine.md §3a).

```
mContribution_i = CM_i x (E_iB - E_iA) / (S_iB - S_iA)
```

`CM_i` en euros/conversion y `(E_B-E_A)/(S_B-S_A)` en conversiones/euro dan
un ratio adimensional: euros de contribucion por euro de gasto marginal.
`1,0` es su punto de equilibrio (cada euro adicional genera exactamente un
euro de margen) -- de ahi que el intervalo de confianza se compare contra
1,0, no contra 0.

Ventanas de 7 dias emparejadas por dia de semana. Dos vetos antes de
calcular nada (profitability-engine.md §3: 'si lo hay, el par se
descarta'): variacion de gasto insuficiente (`|dS|/S_A < 0,15`, sin ella no
hay senal marginal) y cambio estructural declarado por quien arma el par
(creatividad, segmentacion, puja, calendario)."""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from safent_ads.optimization.domain.errors import (
    NonPositiveContributionMarginError,
    PairedWindowShapeError,
)

PAIRED_WINDOW_DAYS = 7
MIN_RELATIVE_SPEND_CHANGE = 0.15
DEFAULT_BOOTSTRAP_ITERATIONS = 1000
DEFAULT_CONFIDENCE = 0.90
BREAKEVEN_RATIO = 1.0


class EstimationMethod(StrEnum):
    PAIRED = "paired"
    MMM = "mmm"
    SHRUNK = "shrunk"


class VetoReason(StrEnum):
    INSUFFICIENT_SPEND_VARIATION = "insufficient_spend_variation"
    STRUCTURAL_CHANGE = "structural_change"


@dataclass(frozen=True, kw_only=True, slots=True)
class DailyPair:
    """Un dia de la ventana A emparejado con el mismo dia de semana de la
    ventana B. `conversions_*` son conversiones de negocio ya proyectadas
    (economics §2), nunca el observado crudo."""

    spend_a: Decimal
    spend_b: Decimal
    conversions_a: float
    conversions_b: float


@dataclass(frozen=True, kw_only=True, slots=True)
class MarginalEstimate:
    """`mContribution` con su intervalo (profitability-engine.md §3, §8
    `get_marginal_roas`: 'valor, IC, metodo paired|mmm|shrunk')."""

    value: float
    ci_low: float
    ci_high: float
    method: EstimationMethod
    sample_size: int

    @property
    def inconclusive(self) -> bool:
        """El IC cruza el punto de equilibrio 1,0: no hay senal suficiente
        para decidir si el euro marginal crea o destruye contribucion."""
        return self.ci_low < BREAKEVEN_RATIO < self.ci_high

    @property
    def destroys_contribution(self) -> bool:
        return not self.inconclusive and self.ci_high <= BREAKEVEN_RATIO


@dataclass(frozen=True, kw_only=True, slots=True)
class PairedWindowVerdict:
    """Resultado de intentar estimar sobre un par de ventanas: o hay
    estimacion, o hay un veto con su motivo (nunca ambos)."""

    estimate: MarginalEstimate | None
    veto_reason: VetoReason | None

    @classmethod
    def accepted(cls, estimate: MarginalEstimate) -> PairedWindowVerdict:
        return cls(estimate=estimate, veto_reason=None)

    @classmethod
    def vetoed(cls, reason: VetoReason) -> PairedWindowVerdict:
        return cls(estimate=None, veto_reason=reason)

    @property
    def is_usable(self) -> bool:
        return self.estimate is not None


def estimate_marginal_contribution_paired(
    *,
    pairs: tuple[DailyPair, ...],
    contribution_margin_per_conversion: Decimal,
    has_structural_change: bool,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence: float = DEFAULT_CONFIDENCE,
    rng: random.Random | None = None,
) -> PairedWindowVerdict:
    if len(pairs) != PAIRED_WINDOW_DAYS:
        raise PairedWindowShapeError(f"se esperan {PAIRED_WINDOW_DAYS} pares, hay {len(pairs)}")
    if contribution_margin_per_conversion <= 0:
        raise NonPositiveContributionMarginError(
            f"contribution_margin_per_conversion <= 0: {contribution_margin_per_conversion}"
        )
    if has_structural_change:
        return PairedWindowVerdict.vetoed(VetoReason.STRUCTURAL_CHANGE)

    spend_a_total = sum((pair.spend_a for pair in pairs), Decimal("0"))
    spend_b_total = sum((pair.spend_b for pair in pairs), Decimal("0"))
    if spend_a_total <= 0 or _relative_spend_change(spend_a_total, spend_b_total) < (
        MIN_RELATIVE_SPEND_CHANGE
    ):
        return PairedWindowVerdict.vetoed(VetoReason.INSUFFICIENT_SPEND_VARIATION)

    cm = float(contribution_margin_per_conversion)
    active_rng = rng or random.Random()  # noqa: S311 - bootstrap estadistico, no criptografia
    point_values = _bootstrap_replicates(
        pairs=pairs, contribution_margin=cm, iterations=bootstrap_iterations, rng=active_rng
    )
    point_estimate = _marginal_contribution(pairs, cm)
    ci_low, ci_high = _percentile_interval(point_values, confidence)
    estimate = MarginalEstimate(
        value=point_estimate,
        ci_low=ci_low,
        ci_high=ci_high,
        method=EstimationMethod.PAIRED,
        sample_size=len(pairs),
    )
    return PairedWindowVerdict.accepted(estimate)


def _relative_spend_change(spend_a: Decimal, spend_b: Decimal) -> float:
    return abs(float(spend_b - spend_a)) / float(spend_a)


def _marginal_contribution(pairs: tuple[DailyPair, ...], contribution_margin: float) -> float:
    spend_a = float(sum((p.spend_a for p in pairs), Decimal("0")))
    spend_b = float(sum((p.spend_b for p in pairs), Decimal("0")))
    conversions_a = sum(p.conversions_a for p in pairs)
    conversions_b = sum(p.conversions_b for p in pairs)
    delta_spend = spend_b - spend_a
    if delta_spend == 0:
        return 0.0
    return contribution_margin * (conversions_b - conversions_a) / delta_spend


def _bootstrap_replicates(
    *, pairs: tuple[DailyPair, ...], contribution_margin: float, iterations: int, rng: random.Random
) -> list[float]:
    replicates: list[float] = []
    for _ in range(iterations):
        resampled = tuple(rng.choices(pairs, k=len(pairs)))
        spend_a = float(sum((p.spend_a for p in resampled), Decimal("0")))
        spend_b = float(sum((p.spend_b for p in resampled), Decimal("0")))
        delta_spend = spend_b - spend_a
        if delta_spend == 0:
            continue
        conversions_a = sum(p.conversions_a for p in resampled)
        conversions_b = sum(p.conversions_b for p in resampled)
        replicates.append(contribution_margin * (conversions_b - conversions_a) / delta_spend)
    return replicates or [_marginal_contribution(pairs, contribution_margin)]


def _percentile_interval(values: list[float], confidence: float) -> tuple[float, float]:
    ordered = sorted(values)
    tail = (1 - confidence) / 2
    low_index = max(0, round(tail * (len(ordered) - 1)))
    high_index = min(len(ordered) - 1, round((1 - tail) * (len(ordered) - 1)))
    return ordered[low_index], ordered[high_index]


def shrink_towards_average_roas(
    *,
    campaign_roas: float,
    campaign_sample_size: int,
    portfolio_average_roas: float,
    prior_strength: int = 15,
) -> float:
    """Degradacion final de la escalera (§3): `r_hat = w*r + (1-w)*r_bar`,
    `w = n/(n+15)`, cuando ni el tactico ni PyMC-Marketing dan senal."""
    weight = campaign_sample_size / (campaign_sample_size + prior_strength)
    return weight * campaign_roas + (1 - weight) * portfolio_average_roas


def marginal_roas_median(estimates: tuple[MarginalEstimate, ...]) -> float | None:
    """Mediana de estimaciones (usada para depurar en tests y para el
    ranking de campanas dentro del mismo grupo `cannibal_group`)."""
    if not estimates:
        return None
    return statistics.median(e.value for e in estimates)
