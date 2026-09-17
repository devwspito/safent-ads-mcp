"""`DiffInDifferencesEvaluator` (tasks.md T198): satisface
`ExperimentEvaluationPort` sin I/O ni dependencias nuevas."""

from __future__ import annotations

import random

import pytest

from safent_ads.optimization.domain.experiment_evaluation import (
    EvaluationMethod,
    QuasiExperimentInput,
)
from safent_ads.optimization.infrastructure.diff_in_differences_evaluator import (
    DiffInDifferencesEvaluator,
)


def test_evaluate_delegates_to_the_pure_domain_function() -> None:
    evaluator = DiffInDifferencesEvaluator(rng=random.Random(1))  # noqa: S311 - test determinista
    inputs = QuasiExperimentInput(
        treatment_pre=(10.0, 10.0),
        treatment_post=(20.0, 20.0),
        control_pre=(5.0, 5.0),
        control_post=(5.0, 5.0),
    )

    result = evaluator.evaluate(inputs)

    assert result.method is EvaluationMethod.DIFFERENCE_IN_DIFFERENCES
    assert result.treatment_effect == pytest.approx(10.0)
    assert result.degraded is True
