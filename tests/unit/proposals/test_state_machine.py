"""`Proposal` — maquina de 9 estados + `SCHEDULED` (T057)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import (
    MAX_OWNER_CONTEXT_LENGTH,
    DiffChangedError,
    PostponedReason,
    Proposal,
    ProposalInvalidated,
    ProposalInvariantError,
    ProposalState,
)

from .conftest import NOW, budget_diff, make_proposal


def _at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


class TestFullLifecycle:
    def test_pending_to_approved_to_scheduled_to_executing_to_executed(self) -> None:
        proposal = make_proposal()

        proposal.approve(proposal.diff.diff_hash, NOW)
        assert proposal.state is ProposalState.APPROVED

        scheduled_at = proposal.schedule_execution(1800, NOW)
        assert proposal.state is ProposalState.SCHEDULED
        assert scheduled_at == NOW + timedelta(seconds=1800)

        proposal.begin_execution(scheduled_at)
        assert proposal.state is ProposalState.EXECUTING

        proposal.record_execution(success=True, now=scheduled_at)
        assert proposal.state is ProposalState.EXECUTED

    def test_executing_can_fail_instead_of_succeed(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)
        proposal.schedule_execution(0, NOW)
        proposal.begin_execution(NOW)

        proposal.record_execution(success=False, now=NOW, error="broker_timeout")

        assert proposal.state is ProposalState.FAILED

    def test_pending_rejects_directly(self) -> None:
        proposal = make_proposal()

        proposal.reject(NOW, reason="no vale la pena")

        assert proposal.state is ProposalState.REJECTED

    def test_pending_expires_directly(self) -> None:
        proposal = make_proposal()

        proposal.expire(NOW)

        assert proposal.state is ProposalState.EXPIRED

    def test_postpone_then_reactivate_returns_to_pending(self) -> None:
        proposal = make_proposal()

        proposal.postpone(_at(3600), NOW)
        assert proposal.state is ProposalState.POSTPONED

        proposal.reactivate(_at(3600))
        assert proposal.state is ProposalState.PENDING
        assert proposal.postpone_until is None
        assert proposal.postponed_reason is None

    def test_postpone_then_reactivate_after_ttl_expires(self) -> None:
        proposal = make_proposal(ttl_hours=1)

        proposal.postpone(_at(1800), NOW)
        proposal.reactivate(_at(7200))

        assert proposal.state is ProposalState.EXPIRED

    def test_approved_can_be_invalidated_by_drift(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)

        proposal.invalidate("platform_state_drifted", NOW)

        assert proposal.state is ProposalState.INVALIDATED

    def test_scheduled_can_be_invalidated_by_undo(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)
        proposal.schedule_execution(1800, NOW)

        proposal.invalidate("undone_within_grace", NOW)

        assert proposal.state is ProposalState.INVALIDATED


class TestInvalidTransitionsAreRejected:
    @pytest.mark.parametrize(
        "setup_state",
        [
            ProposalState.REJECTED,
            ProposalState.EXPIRED,
            ProposalState.EXECUTED,
            ProposalState.FAILED,
            ProposalState.INVALIDATED,
        ],
    )
    def test_terminal_states_accept_no_further_transition(
        self, setup_state: ProposalState
    ) -> None:
        proposal = make_proposal()
        proposal.state = setup_state

        with pytest.raises(ProposalInvariantError):
            proposal.approve(proposal.diff.diff_hash, NOW)

    def test_cannot_approve_twice(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)

        with pytest.raises(ProposalInvariantError):
            proposal.approve(proposal.diff.diff_hash, NOW)

    def test_cannot_begin_execution_before_scheduled(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)

        with pytest.raises(ProposalInvariantError):
            proposal.begin_execution(NOW)

    def test_begin_execution_rejects_if_grace_window_not_elapsed(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)
        proposal.schedule_execution(1800, NOW)

        with pytest.raises(ProposalInvariantError, match="ventana de gracia"):
            proposal.begin_execution(_at(60))

    def test_begin_execution_rejects_if_expired_even_when_scheduled(self) -> None:
        proposal = make_proposal(ttl_hours=1)
        proposal.approve(proposal.diff.diff_hash, NOW)
        proposal.schedule_execution(0, NOW)

        with pytest.raises(ProposalInvariantError, match="caducada"):
            proposal.begin_execution(_at(7200))


class TestApprovalDiffBinding:
    def test_approve_rejects_stale_diff_hash(self) -> None:
        proposal = make_proposal()

        with pytest.raises(DiffChangedError):
            proposal.approve("stale-hash", NOW)

        assert proposal.state is ProposalState.PENDING


class TestEditProposedValueInvalidatesAuthorization:
    def test_edit_while_pending_just_recomputes_hash(self) -> None:
        proposal = make_proposal()
        original_hash = proposal.diff.diff_hash

        new_diff = proposal.edit_proposed_value(Money.of("55"), NOW)

        assert new_diff.diff_hash != original_hash
        assert proposal.state is ProposalState.PENDING

    def test_edit_while_approved_reverts_to_pending_and_emits_event(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)
        proposal.pull_events()

        proposal.edit_proposed_value(Money.of("55"), NOW)

        assert proposal.state is ProposalState.PENDING
        events = proposal.pull_events()
        assert any(isinstance(event, ProposalInvalidated) for event in events)

    def test_edit_while_scheduled_reverts_to_pending_and_clears_schedule(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)
        proposal.schedule_execution(1800, NOW)

        proposal.edit_proposed_value(Money.of("55"), NOW)

        assert proposal.state is ProposalState.PENDING
        assert proposal.execution_scheduled_at is None

    def test_edit_after_execution_raises(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)
        proposal.schedule_execution(0, NOW)
        proposal.begin_execution(NOW)
        proposal.record_execution(success=True, now=NOW)

        with pytest.raises(ProposalInvariantError):
            proposal.edit_proposed_value(Money.of("55"), NOW)


class TestIsExpired:
    def test_not_expired_before_deadline(self) -> None:
        proposal = make_proposal(ttl_hours=1)

        assert proposal.is_expired(_at(1800)) is False

    def test_expired_at_or_after_deadline(self) -> None:
        proposal = make_proposal(ttl_hours=1)

        assert proposal.is_expired(_at(3600)) is True
        assert proposal.is_expired(_at(7200)) is True


class TestDiffHashIsCanonical:
    def test_same_inputs_produce_same_hash(self) -> None:
        first = budget_diff()
        second = budget_diff()

        assert first.diff_hash == second.diff_hash

    def test_different_after_value_changes_hash(self) -> None:
        first = budget_diff(after="70")
        second = budget_diff(after="71")

        assert first.diff_hash != second.diff_hash


def test_proposal_class_is_importable_standalone() -> None:
    # smoke: modulo cargable de forma aislada, sin ciclos con execution/.
    assert Proposal is not None


class TestPostponedReason:
    def test_postpone_defaults_to_owner_reason(self) -> None:
        proposal = make_proposal()

        proposal.postpone(_at(3600), NOW)

        assert proposal.postponed_reason is PostponedReason.OWNER

    def test_postpone_accepts_explicit_reason(self) -> None:
        proposal = make_proposal()

        proposal.postpone(_at(3600), NOW, reason=PostponedReason.ATTENTION_BUDGET)

        assert proposal.postponed_reason is PostponedReason.ATTENTION_BUDGET

    def test_postpone_of_a_non_pending_proposal_raises(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)

        with pytest.raises(ProposalInvariantError):
            proposal.postpone(_at(3600), NOW)


class TestOwnerContext:
    def test_set_owner_context_stores_the_text(self) -> None:
        proposal = make_proposal()

        proposal.set_owner_context("Esperar a que cierre la campaña de verano.")

        assert proposal.owner_context == "Esperar a que cierre la campaña de verano."

    def test_set_owner_context_accepts_exactly_the_max_length(self) -> None:
        proposal = make_proposal()
        text = "a" * MAX_OWNER_CONTEXT_LENGTH

        proposal.set_owner_context(text)

        assert proposal.owner_context == text

    def test_set_owner_context_rejects_text_over_the_max_length(self) -> None:
        proposal = make_proposal()
        text = "a" * (MAX_OWNER_CONTEXT_LENGTH + 1)

        with pytest.raises(ProposalInvariantError):
            proposal.set_owner_context(text)

    def test_set_owner_context_works_regardless_of_state(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)

        proposal.set_owner_context("nota tras aprobar")

        assert proposal.owner_context == "nota tras aprobar"
        assert proposal.state is ProposalState.APPROVED
