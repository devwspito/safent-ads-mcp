"""FR-20: `is_equivalent_to` y `update_with_equivalent` — "una sola propuesta
abierta por (entity_ref, parametro): la nueva actualiza la existente"."""

from __future__ import annotations

from datetime import timedelta

from safent_ads.proposals.domain.cause import Cause, Evidence
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import (
    ProposalState,
    ProposalSupersededByEquivalent,
    ProposedDiff,
)

from .conftest import NOW, budget_diff, cause_key, entity_ref, make_proposal


class TestIsEquivalentTo:
    def test_same_entity_and_parameter_is_equivalent(self) -> None:
        first = make_proposal(diff=budget_diff(after="70"))
        second = make_proposal(diff=budget_diff(after="65"))

        assert first.is_equivalent_to(second) is True

    def test_different_entity_is_not_equivalent(self) -> None:
        first = make_proposal(diff=budget_diff(ref=entity_ref("111")))
        second = make_proposal(diff=budget_diff(ref=entity_ref("222")))

        assert first.is_equivalent_to(second) is False

    def test_different_parameter_same_entity_is_not_equivalent(self) -> None:
        ref = entity_ref()
        first = make_proposal(diff=budget_diff(ref=ref))
        pause_diff = ProposedDiff.build(ref, "status", "ACTIVE", "PAUSED")
        second = make_proposal(diff=pause_diff)

        assert first.is_equivalent_to(second) is False


class TestEquivalentProposalUpdatesNotDuplicates:
    def test_equivalent_proposal_updates_not_duplicates(self) -> None:
        existing = make_proposal(diff=budget_diff(after="70"))
        original_id = existing.proposal_id
        original_hash = existing.diff.diff_hash

        incoming_diff = budget_diff(after="65")
        new_expires_at = NOW + timedelta(hours=24)

        existing.update_with_equivalent(
            new_diff=incoming_diff,
            new_cause=Cause(text="CPL sigue sobre objetivo, empeora", rule_id="M05"),
            new_evidence=(Evidence(metric="cpl", actual=45.0, target=28.0, window_preset="7D"),),
            new_estimated_impact=Money.of("340"),
            new_expires_at=new_expires_at,
            now=NOW,
        )

        # Sigue siendo la MISMA propuesta (mismo id) con el diff actualizado —
        # no aparece una segunda propuesta para (entity_ref, parametro).
        assert existing.proposal_id == original_id
        assert existing.diff.diff_hash != original_hash
        assert existing.diff.after == Money.of("65")
        assert existing.state is ProposalState.PENDING
        assert existing.expires_at == new_expires_at

    def test_absorbing_reactivates_a_postponed_proposal(self) -> None:
        existing = make_proposal(diff=budget_diff(after="70"))
        existing.postpone(NOW + timedelta(hours=1), NOW)

        existing.update_with_equivalent(
            new_diff=budget_diff(after="65"),
            new_cause=Cause(text="Reevaluado: sigue sobre objetivo", rule_id="M05"),
            new_evidence=(),
            new_estimated_impact=Money.of("340"),
            new_expires_at=NOW + timedelta(hours=24),
            now=NOW,
        )

        assert existing.state is ProposalState.PENDING
        assert existing.postpone_until is None

    def test_absorbing_emits_superseded_event(self) -> None:
        existing = make_proposal(diff=budget_diff(after="70"))
        existing.pull_events()

        existing.update_with_equivalent(
            new_diff=budget_diff(after="65"),
            new_cause=Cause(text="Nueva causa equivalente", rule_id="M05"),
            new_evidence=(),
            new_estimated_impact=Money.of("340"),
            new_expires_at=NOW + timedelta(hours=24),
            now=NOW,
        )

        events = existing.pull_events()
        assert any(isinstance(event, ProposalSupersededByEquivalent) for event in events)

    def test_cause_key_grouping_key_is_stable_for_equivalent_causes(self) -> None:
        key = cause_key()

        assert key.as_grouping_key() == cause_key().as_grouping_key()
