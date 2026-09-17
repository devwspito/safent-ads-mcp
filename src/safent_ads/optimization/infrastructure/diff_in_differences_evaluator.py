"""`ExperimentEvaluationPort` real, T198: unico adaptador hoy, puro (sin
`tfcausalimpact` -- no es dependencia, ver `experiment_evaluation.py`).
Vive en `infrastructure/` como el resto de adaptadores concretos aunque no
haga I/O: el dia que llegue un adaptador sobre `tfcausalimpact` (aprobado
explicitamente, con su mantenimiento asumido), sustituye a este sin tocar
`ExperimentEvaluationPort` ni a quien lo llama."""

from __future__ import annotations

import random

from safent_ads.optimization.domain.experiment_evaluation import (
    QuasiExperimentInput,
    QuasiExperimentResult,
    evaluate_quasi_experiment,
)

__all__ = ["DiffInDifferencesEvaluator"]


class DiffInDifferencesEvaluator:
    """`optimization.application.ports.ExperimentEvaluationPort`."""

    def __init__(self, *, rng: random.Random | None = None) -> None:
        self._rng = rng

    def evaluate(self, inputs: QuasiExperimentInput) -> QuasiExperimentResult:
        return evaluate_quasi_experiment(inputs, rng=self._rng)
