"""`evaluate_quasi_experiment` (profitability-engine.md §4, tasks.md T198):
diferencia en diferencias, el fallback puro cuando `tfcausalimpact` no es
dependencia (no lo es -- ver docstring del modulo). Siempre `degraded=True`:
nunca se presenta como un experimento aleatorizado."""

from __future__ import annotations

import random

import pytest

from safent_ads.optimization.domain.errors import PairedWindowShapeError
from safent_ads.optimization.domain.experiment_evaluation import (
    EvaluationMethod,
    QuasiExperimentInput,
    evaluate_quasi_experiment,
)


def _fixed_rng() -> random.Random:
    return random.Random(7)  # noqa: S311 - reproducibilidad de test, no criptografia


class TestEvaluateQuasiExperiment:
    def test_positive_treatment_effect_net_of_the_control_trend(self) -> None:
        # Tratamiento sube 20, control (sin cambio) sube 5 por su cuenta:
        # el efecto neto atribuible es 15.
        inputs = QuasiExperimentInput(
            treatment_pre=(100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0),
            treatment_post=(120.0, 120.0, 120.0, 120.0, 120.0, 120.0, 120.0),
            control_pre=(80.0, 80.0, 80.0, 80.0, 80.0, 80.0, 80.0),
            control_post=(85.0, 85.0, 85.0, 85.0, 85.0, 85.0, 85.0),
        )

        result = evaluate_quasi_experiment(inputs, rng=_fixed_rng())

        assert result.method is EvaluationMethod.DIFFERENCE_IN_DIFFERENCES
        assert result.treatment_effect == pytest.approx(15.0)
        assert result.degraded is True
        assert result.sample_size == 7

    def test_no_effect_when_both_arms_move_together(self) -> None:
        inputs = QuasiExperimentInput(
            treatment_pre=(100.0, 100.0, 100.0),
            treatment_post=(110.0, 110.0, 110.0),
            control_pre=(50.0, 50.0, 50.0),
            control_post=(60.0, 60.0, 60.0),
        )

        result = evaluate_quasi_experiment(inputs, rng=_fixed_rng())

        assert result.treatment_effect == pytest.approx(0.0)
        assert result.ci_low <= 0.0 <= result.ci_high

    def test_rejects_mismatched_series_lengths(self) -> None:
        with pytest.raises(PairedWindowShapeError):
            QuasiExperimentInput(
                treatment_pre=(1.0, 2.0),
                treatment_post=(1.0,),
                control_pre=(1.0, 2.0),
                control_post=(1.0, 2.0),
            )

    def test_rejects_empty_series(self) -> None:
        with pytest.raises(PairedWindowShapeError):
            QuasiExperimentInput(
                treatment_pre=(), treatment_post=(), control_pre=(), control_post=()
            )

    def test_result_is_always_labeled_degraded(self) -> None:
        """§4: 'entra como quasi_experiment... degradado y etiquetado' --
        nunca se puede construir un resultado no degradado desde este
        fallback."""
        inputs = QuasiExperimentInput(
            treatment_pre=(10.0,), treatment_post=(20.0,), control_pre=(5.0,), control_post=(5.0,)
        )

        result = evaluate_quasi_experiment(inputs, rng=_fixed_rng())

        assert result.degraded is True
