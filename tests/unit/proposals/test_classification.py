"""`Classification`/`ClassificationPolicy` — FR-12 mas techo `CRITICAL`."""

from __future__ import annotations

from safent_ads.proposals.domain.classification import (
    Classification,
    ClassificationPolicy,
    ProposalKind,
)
from safent_ads.proposals.domain.money import Money

_POLICY = ClassificationPolicy(critical_impact_threshold=Money.of("1000"))


class TestFr12AlwaysImportantKinds:
    def test_budget_increase_is_important(self) -> None:
        result = _POLICY.classify(ProposalKind.BUDGET_INCREASE, Money.of("50"))

        assert result is Classification.IMPORTANT

    def test_create_campaign_is_important(self) -> None:
        result = _POLICY.classify(ProposalKind.CREATE_CAMPAIGN, Money.of("0"))

        assert result is Classification.IMPORTANT

    def test_targeting_change_is_important(self) -> None:
        result = _POLICY.classify(ProposalKind.TARGETING_CHANGE, Money.of("0"))

        assert result is Classification.IMPORTANT

    def test_creative_publication_is_important(self) -> None:
        result = _POLICY.classify(ProposalKind.CREATIVE_PUBLICATION, Money.of("0"))

        assert result is Classification.IMPORTANT

    def test_experiment_is_important(self) -> None:
        """profitability-engine.md §4: `propose_experiment` nunca corre
        autonomo -- IMPORTANT exige aprobacion humana (FR-12)."""
        result = _POLICY.classify(ProposalKind.EXPERIMENT, Money.of("0"))

        assert result is Classification.IMPORTANT

    def test_delete_is_important(self) -> None:
        """design.md §0.7: borrar es irreversible --
        nunca RUTINARIA, aunque su impacto estimado (`Money.zero()`,
        `entity_lifecycle_actions.py`) quede muy por debajo del techo."""
        result = _POLICY.classify(ProposalKind.DELETE, Money.of("0"))

        assert result is Classification.IMPORTANT


class TestDefensiveKindsAreRoutineUnderThreshold:
    def test_budget_decrease_is_routine(self) -> None:
        result = _POLICY.classify(ProposalKind.BUDGET_DECREASE, Money.of("50"))

        assert result is Classification.ROUTINE

    def test_pause_is_routine(self) -> None:
        result = _POLICY.classify(ProposalKind.PAUSE, Money.of("0"))

        assert result is Classification.ROUTINE


class TestCriticalThresholdEscalatesAnyKind:
    def test_defensive_kind_over_threshold_is_critical(self) -> None:
        result = _POLICY.classify(ProposalKind.BUDGET_DECREASE, Money.of("1500"))

        assert result is Classification.CRITICAL

    def test_important_kind_over_threshold_is_critical_not_important(self) -> None:
        result = _POLICY.classify(ProposalKind.BUDGET_INCREASE, Money.of("2000"))

        assert result is Classification.CRITICAL

    def test_exactly_at_threshold_is_critical(self) -> None:
        result = _POLICY.classify(ProposalKind.PAUSE, Money.of("1000"))

        assert result is Classification.CRITICAL
