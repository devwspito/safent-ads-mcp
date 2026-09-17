"""Evaluacion de un `quasi_experiment` (profitability-engine.md §4:
"apagar y encender comparando antes/despues no es experimento: entra como
quasi_experiment con tfcausalimpact, degradado y etiquetado").

**T198, decision honesta**: `tfcausalimpact` no es dependencia de
`pyproject.toml` (research/reuse-bases.md: "reuse con reserva, 20 meses
sin commit, lo pineamos y asumimos mantenimiento") y no hay adaptador que
lo importe. No se anade aqui -- una dependencia pesada, con mantenimiento
que pasaria a ser nuestro, no entra sin que alguien lo autorice
explicitamente. Este modulo implementa SOLO el fallback puro (diferencia
en diferencias): sin `statsmodels`/TensorFlow, sin series sinteticas de
control bayesianas, con bootstrap sobre los mismos dias pareados que ya
usa `marginal.py` para §3a. `EvaluationMethod.DIFFERENCE_IN_DIFFERENCES`
siempre marca `degraded=True`: un resultado de este modulo nunca se
presenta como si fuera un experimento aleatorizado."""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from enum import StrEnum

from safent_ads.optimization.domain.errors import PairedWindowShapeError

DEFAULT_BOOTSTRAP_ITERATIONS = 1000
DEFAULT_CONFIDENCE = 0.90


class EvaluationMethod(StrEnum):
    CAUSAL_IMPACT = "causal_impact"  # tfcausalimpact -- sin adaptador hoy, ver docstring del modulo
    DIFFERENCE_IN_DIFFERENCES = "difference_in_differences"


@dataclass(frozen=True, kw_only=True, slots=True)
class QuasiExperimentInput:
    """Series diarias emparejadas por dia de semana (mismo criterio que
    `marginal.DailyPair`): el `quasi_experiment` mas simple es
    tratamiento/control antes/despues del cambio."""

    treatment_pre: tuple[float, ...]
    treatment_post: tuple[float, ...]
    control_pre: tuple[float, ...]
    control_post: tuple[float, ...]

    def __post_init__(self) -> None:
        lengths = {
            len(self.treatment_pre),
            len(self.treatment_post),
            len(self.control_pre),
            len(self.control_post),
        }
        if len(lengths) != 1 or lengths == {0}:
            raise PairedWindowShapeError(
                "quasi_experiment exige las 4 series con la misma longitud, al menos 1 dia"
            )


@dataclass(frozen=True, kw_only=True, slots=True)
class QuasiExperimentResult:
    """`degraded=True` siempre (§4): este resultado nunca sustituye a un
    `Experiment` aleatorizado, solo informa con la etiqueta puesta."""

    method: EvaluationMethod
    treatment_effect: float
    ci_low: float
    ci_high: float
    sample_size: int
    degraded: bool = True


def evaluate_quasi_experiment(
    inputs: QuasiExperimentInput,
    *,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence: float = DEFAULT_CONFIDENCE,
    rng: random.Random | None = None,
) -> QuasiExperimentResult:
    """Diferencia en diferencias: `(post_T - pre_T) - (post_C - pre_C)`.
    El control absorbe lo que le hubiera pasado al tratamiento sin el
    cambio -- la misma logica que separa senal de estacionalidad en
    `diagnosis.py` nodo 9, aplicada a dos series en vez de a una."""
    active_rng = rng or random.Random()  # noqa: S311 - bootstrap estadistico, no criptografia
    n = len(inputs.treatment_pre)
    point_effect = _diff_in_diff(inputs)
    replicates = [
        _diff_in_diff(_resample(inputs, active_rng)) for _ in range(bootstrap_iterations)
    ]
    ci_low, ci_high = _percentile_interval(replicates, confidence)
    return QuasiExperimentResult(
        method=EvaluationMethod.DIFFERENCE_IN_DIFFERENCES,
        treatment_effect=point_effect,
        ci_low=ci_low,
        ci_high=ci_high,
        sample_size=n,
    )


def _diff_in_diff(inputs: QuasiExperimentInput) -> float:
    treatment_delta = statistics.fmean(inputs.treatment_post) - statistics.fmean(
        inputs.treatment_pre
    )
    control_delta = statistics.fmean(inputs.control_post) - statistics.fmean(inputs.control_pre)
    return treatment_delta - control_delta


def _resample(inputs: QuasiExperimentInput, rng: random.Random) -> QuasiExperimentInput:
    n = len(inputs.treatment_pre)
    indices = [rng.randrange(n) for _ in range(n)]
    return QuasiExperimentInput(
        treatment_pre=tuple(inputs.treatment_pre[i] for i in indices),
        treatment_post=tuple(inputs.treatment_post[i] for i in indices),
        control_pre=tuple(inputs.control_pre[i] for i in indices),
        control_post=tuple(inputs.control_post[i] for i in indices),
    )


def _percentile_interval(values: list[float], confidence: float) -> tuple[float, float]:
    ordered = sorted(values)
    tail = (1 - confidence) / 2
    low_index = max(0, round(tail * (len(ordered) - 1)))
    high_index = min(len(ordered) - 1, round((1 - tail) * (len(ordered) - 1)))
    return ordered[low_index], ordered[high_index]
