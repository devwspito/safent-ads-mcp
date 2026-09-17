"""Puertas de `signals`: una fallida siempre bloquea la accionabilidad
(`test_learning_entity_never_actionable`, data-model.md §Signal)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from safent_ads.signals.domain.gate_verdict import GateName
from safent_ads.signals.domain.gates import (
    AttributionLagGate,
    CooldownGate,
    LearningGate,
    LearningStatus,
    MinDataGate,
)


@pytest.mark.parametrize("status", [LearningStatus.LEARNING, LearningStatus.FAIL])
def test_learning_entity_never_actionable(status: LearningStatus) -> None:
    verdict = LearningGate.evaluate(status)

    assert verdict.passed is False
    assert verdict.gate == GateName.LEARNING
    assert verdict.reason is not None


def test_learning_gate_passes_on_success() -> None:
    verdict = LearningGate.evaluate(LearningStatus.SUCCESS)

    assert verdict.passed is True


def test_min_data_gate_passes_on_spend_and_impressions() -> None:
    verdict = MinDataGate.evaluate(
        spend_minor=25_000,
        target_cpa_minor=2_500,
        impressions=1_500,
        conversions=0,
        min_spend_multiple=5.0,
        min_impressions=1_000,
        min_conversions=10,
    )

    assert verdict.passed is True


def test_min_data_gate_passes_on_conversion_count_alone() -> None:
    verdict = MinDataGate.evaluate(
        spend_minor=100,
        target_cpa_minor=2_500,
        impressions=10,
        conversions=10,
        min_spend_multiple=5.0,
        min_impressions=1_000,
        min_conversions=10,
    )

    assert verdict.passed is True


def test_min_data_gate_blocks_when_neither_condition_met() -> None:
    verdict = MinDataGate.evaluate(
        spend_minor=100,
        target_cpa_minor=2_500,
        impressions=10,
        conversions=1,
        min_spend_multiple=5.0,
        min_impressions=1_000,
        min_conversions=10,
    )

    assert verdict.passed is False


def test_attribution_lag_gate_blocks_within_median_lag() -> None:
    verdict = AttributionLagGate.evaluate(
        window_end=date(2026, 9, 8), median_lag_days=3, as_of=date(2026, 9, 9)
    )

    assert verdict.passed is False


def test_attribution_lag_gate_passes_after_median_lag() -> None:
    verdict = AttributionLagGate.evaluate(
        window_end=date(2026, 9, 5), median_lag_days=3, as_of=date(2026, 9, 9)
    )

    assert verdict.passed is True


def test_cooldown_gate_passes_with_no_prior_change() -> None:
    verdict = CooldownGate.evaluate(
        last_change_at=None, cooldown=timedelta(hours=24), as_of=datetime(2026, 9, 9, tzinfo=UTC)
    )

    assert verdict.passed is True


def test_cooldown_gate_blocks_inside_window() -> None:
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    verdict = CooldownGate.evaluate(
        last_change_at=now - timedelta(hours=1), cooldown=timedelta(hours=24), as_of=now
    )

    assert verdict.passed is False


def test_cooldown_gate_passes_at_exact_boundary() -> None:
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    verdict = CooldownGate.evaluate(
        last_change_at=now - timedelta(hours=24), cooldown=timedelta(hours=24), as_of=now
    )

    assert verdict.passed is True
