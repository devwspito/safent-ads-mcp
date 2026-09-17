"""`resolve_correctness` (profitability-engine.md §6, tasks.md T199): un
caso por celda de la tabla de decision -- nunca se inventa
confirmado/contradicho cuando el dato falta o el caso es de
auto-confirmacion."""

from __future__ import annotations

import pytest

from safent_ads.optimization.domain.calibration import OutcomeSource
from safent_ads.optimization.domain.signal_outcome_evaluation import (
    OutcomeVerdict,
    resolve_correctness,
    verdict_of,
)
from safent_ads.rules.domain.autonomy import ActionKind


class TestDefensiveRule:
    """SELL/EXIT/... (no sube gasto): avisan de un problema."""

    def test_applied_is_inconclusive_auto_confirmation_guard(self) -> None:
        assert (
            resolve_correctness(
                action_kind=ActionKind.SELL,
                outcome_source=OutcomeSource.APPLIED,
                entity_is_bad=True,
            )
            is None
        )

    @pytest.mark.parametrize("source", [OutcomeSource.EXPIRED, OutcomeSource.REJECTED])
    def test_control_group_confirms_when_entity_turned_bad(
        self, source: OutcomeSource
    ) -> None:
        assert (
            resolve_correctness(
                action_kind=ActionKind.EXIT, outcome_source=source, entity_is_bad=True
            )
            is True
        )

    @pytest.mark.parametrize("source", [OutcomeSource.EXPIRED, OutcomeSource.REJECTED])
    def test_control_group_contradicts_when_entity_stayed_healthy(
        self, source: OutcomeSource
    ) -> None:
        assert (
            resolve_correctness(
                action_kind=ActionKind.SELL, outcome_source=source, entity_is_bad=False
            )
            is False
        )


class TestSpendIncreasingRule:
    """BUY/CREATIVE_SCALE/LOOSEN_TARGET: dicen 'escala, va bien'."""

    def test_applied_confirms_when_entity_stayed_healthy(self) -> None:
        assert (
            resolve_correctness(
                action_kind=ActionKind.BUY,
                outcome_source=OutcomeSource.APPLIED,
                entity_is_bad=False,
            )
            is True
        )

    def test_applied_contradicts_when_entity_turned_bad(self) -> None:
        assert (
            resolve_correctness(
                action_kind=ActionKind.BUY,
                outcome_source=OutcomeSource.APPLIED,
                entity_is_bad=True,
            )
            is False
        )

    @pytest.mark.parametrize("source", [OutcomeSource.EXPIRED, OutcomeSource.REJECTED])
    def test_missed_opportunity_is_inconclusive_no_counterfactual(
        self, source: OutcomeSource
    ) -> None:
        assert (
            resolve_correctness(
                action_kind=ActionKind.BUY, outcome_source=source, entity_is_bad=True
            )
            is None
        )


def test_missing_metrics_is_always_inconclusive() -> None:
    assert (
        resolve_correctness(
            action_kind=ActionKind.SELL,
            outcome_source=OutcomeSource.REJECTED,
            entity_is_bad=None,
        )
        is None
    )


@pytest.mark.parametrize(
    ("was_correct", "expected"),
    [
        (True, OutcomeVerdict.CONFIRMED),
        (False, OutcomeVerdict.CONTRADICTED),
        (None, OutcomeVerdict.INCONCLUSIVE),
    ],
)
def test_verdict_of(was_correct: bool | None, expected: OutcomeVerdict) -> None:
    assert verdict_of(was_correct) is expected
