"""`Experiment` (profitability-engine.md §4): monotonia del calculo de
muestra, diseno imposible rechazado (no arrancado), futilidad, guardarrail."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime

import pytest

from safent_ads.optimization.domain.errors import (
    ImpossibleExperimentDesignError,
    InvalidExperimentTransitionError,
)
from safent_ads.optimization.domain.experiment import (
    DEFAULT_ALPHA,
    ArmResult,
    Experiment,
    ExperimentState,
    SampleSizeDesign,
    conditional_power_below_futility_floor,
    design_sample_size,
    guardrail_breach,
    minimum_duration_days,
    success_stop_reached,
)
from safent_ads.optimization.domain.identifiers import ExperimentId

_NOW = datetime(2026, 9, 9, tzinfo=UTC)


class TestSampleSizeMonotonicity:
    def test_smaller_mde_needs_more_sample(self) -> None:
        big_mde = design_sample_size(
            baseline_rate=0.08, relative_mde=0.25, available_units_per_arm_per_week=1000
        )
        small_mde = design_sample_size(
            baseline_rate=0.08, relative_mde=0.10, available_units_per_arm_per_week=1000
        )
        assert small_mde.sample_per_arm > big_mde.sample_per_arm

    def test_matches_worked_example_orders_of_magnitude(self) -> None:
        # profitability-engine.md §4: conversion de negocio/lead ~6.560/brazo,
        # lead/clic ~2.274/brazo.
        business_conversion_design = design_sample_size(
            baseline_rate=0.045, relative_mde=0.20, available_units_per_arm_per_week=1000
        )
        lead_design = design_sample_size(
            baseline_rate=0.08, relative_mde=0.25, available_units_per_arm_per_week=1000
        )
        assert business_conversion_design.sample_per_arm == pytest.approx(6560, rel=0.01)
        assert lead_design.sample_per_arm == pytest.approx(2274, rel=0.01)


class TestFeasibilityRejection:
    def test_impossible_design_is_rejected_not_started(self) -> None:
        design = SampleSizeDesign(
            baseline_rate=0.045,
            relative_mde=0.20,
            sample_per_arm=6560,
            available_units_per_arm_per_week=100,  # 65.6 semanas -> inviable
        )
        with pytest.raises(ImpossibleExperimentDesignError):
            Experiment.draft(
                experiment_id=ExperimentId.new(),
                hypothesis="subir puja mejora conversiones de negocio",
                metric="business_conversion_rate",
                design=design,
                now=_NOW,
            )

    def test_feasible_design_starts_in_draft(self) -> None:
        design = design_sample_size(
            baseline_rate=0.08, relative_mde=0.25, available_units_per_arm_per_week=1200
        )
        experiment = Experiment.draft(
            experiment_id=ExperimentId.new(),
            hypothesis="nuevo creativo mejora CTR",
            metric="click_to_lead",
            design=design,
            now=_NOW,
        )
        assert experiment.state is ExperimentState.DRAFT
        assert experiment.duration_days <= 28


class TestMinimumDuration:
    def test_business_conversion_metric_adds_median_lag(self) -> None:
        assert (
            minimum_duration_days(metric_measures_business_conversion=True, median_lag_days=20)
            == 27
        )

    def test_non_business_conversion_metric_uses_flat_floor(self) -> None:
        assert (
            minimum_duration_days(metric_measures_business_conversion=False, median_lag_days=20)
            == 7
        )


class TestStateMachine:
    def _running_experiment(self) -> Experiment:
        design = design_sample_size(
            baseline_rate=0.08, relative_mde=0.25, available_units_per_arm_per_week=1200
        )
        experiment = Experiment.draft(
            experiment_id=ExperimentId.new(),
            hypothesis="h",
            metric="click_to_lead",
            design=design,
            now=_NOW,
        )
        experiment.start()
        return experiment

    def test_futility_stop_then_conclude(self) -> None:
        experiment = self._running_experiment()
        experiment.stop_for_futility()
        assert experiment.state is ExperimentState.STOPPED_FUTILITY
        experiment.conclude()
        assert experiment.state is ExperimentState.CONCLUDED

    def test_cannot_conclude_directly_from_running(self) -> None:
        experiment = self._running_experiment()
        with pytest.raises(InvalidExperimentTransitionError):
            experiment.conclude()

    def test_cannot_restart_a_concluded_experiment(self) -> None:
        experiment = self._running_experiment()
        experiment.stop_for_success()
        experiment.conclude()
        with pytest.raises(InvalidExperimentTransitionError):
            experiment.start()


class TestFutilityAndGuardrailStops:
    def test_low_conditional_power_triggers_futility(self) -> None:
        assert conditional_power_below_futility_floor(0.15) is True
        assert conditional_power_below_futility_floor(0.50) is False

    def test_treatment_above_2x_target_cpe_breaches_guardrail(self) -> None:
        assert guardrail_breach(treatment_cpe=1000, target_cpe=400, treatment_conversions=3) is True

    def test_5x_spend_with_zero_conversions_breaches_guardrail(self) -> None:
        assert guardrail_breach(treatment_cpe=2000, target_cpe=400, treatment_conversions=0) is True

    def test_normal_treatment_does_not_breach(self) -> None:
        assert guardrail_breach(treatment_cpe=450, target_cpe=400, treatment_conversions=5) is False


class TestSuccessStop:
    def test_clear_lift_with_ample_sample_reaches_success(self) -> None:
        treatment = ArmResult(conversions=150, sample_size=1000)
        control = ArmResult(conversions=80, sample_size=1000)

        assert success_stop_reached(treatment=treatment, control=control) is True

    def test_identical_arms_never_reach_success(self) -> None:
        treatment = ArmResult(conversions=100, sample_size=1000)
        control = ArmResult(conversions=100, sample_size=1000)

        assert success_stop_reached(treatment=treatment, control=control) is False

    def test_small_difference_within_the_confidence_interval_is_not_success(self) -> None:
        treatment = ArmResult(conversions=102, sample_size=1000)
        control = ArmResult(conversions=100, sample_size=1000)

        assert success_stop_reached(treatment=treatment, control=control) is False

    def test_rejects_out_of_range_conversions(self) -> None:
        with pytest.raises(ImpossibleExperimentDesignError):
            ArmResult(conversions=11, sample_size=10)


class TestAaFalsePositiveRate:
    """profitability-engine.md §4: 'A/A de 1.000 experimentos -> falsos
    positivos <= alpha'. Dos brazos con la MISMA tasa real (sin efecto):
    la parada de exito no debe dispararse mas de lo que alpha permite."""

    def test_1000_aa_replicas_stay_within_the_nominal_alpha_margin(self) -> None:
        rng = random.Random(20260909)  # noqa: S311 - simulacion estadistica, no criptografia
        replicas = 1000
        n_per_arm = 500
        baseline_rate = 0.10

        false_positives = sum(
            success_stop_reached(
                treatment=_simulate_arm(rng, n_per_arm, baseline_rate),
                control=_simulate_arm(rng, n_per_arm, baseline_rate),
                alpha=DEFAULT_ALPHA,
            )
            for _ in range(replicas)
        )

        false_positive_rate = false_positives / replicas
        # Margen de Monte Carlo a 3 sigma sobre la tasa nominal (Wilson):
        # sqrt(alpha*(1-alpha)/replicas) ~= 0.0095 -> +-0.0285.
        margin = 3 * math.sqrt(DEFAULT_ALPHA * (1 - DEFAULT_ALPHA) / replicas)
        assert false_positive_rate <= DEFAULT_ALPHA + margin


def _simulate_arm(rng: random.Random, n: int, rate: float) -> ArmResult:
    conversions = sum(1 for _ in range(n) if rng.random() < rate)
    return ArmResult(conversions=conversions, sample_size=n)
