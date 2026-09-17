"""`WithdrawProposal` (contracts/mcp-tools.md `withdraw_proposal`)."""

from __future__ import annotations

import pytest

from safent_ads.proposals.application.withdraw_proposal import (
    ProposalNotWithdrawableError,
    WithdrawProposal,
    WithdrawProposalCommand,
)
from safent_ads.proposals.domain.proposal import ProposalState, new_proposal_id
from safent_ads.proposals.testing.fakes import FakeProposalRepository
from safent_ads.shared.clock import FixedClock

from .conftest import NOW, make_proposal


def _use_case(proposals: FakeProposalRepository) -> WithdrawProposal:
    return WithdrawProposal(proposals, FixedClock(NOW))


class TestWithdrawPending:
    async def test_a_pending_proposal_is_rejected(self) -> None:
        proposal = make_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)

        state = await _use_case(proposals).execute(
            WithdrawProposalCommand(proposal.proposal_id, reason="ya no aplica")
        )

        assert state is ProposalState.REJECTED
        stored = await proposals.get(proposal.proposal_id)
        assert stored is not None
        assert stored.state is ProposalState.REJECTED


class TestWithdrawApprovedOrScheduled:
    async def test_an_approved_proposal_is_invalidated(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)
        proposals = FakeProposalRepository()
        await proposals.save(proposal)

        state = await _use_case(proposals).execute(
            WithdrawProposalCommand(proposal.proposal_id)
        )

        assert state is ProposalState.INVALIDATED


class TestWithdrawNotAllowed:
    async def test_a_missing_proposal_raises(self) -> None:
        with pytest.raises(ProposalNotWithdrawableError):
            await _use_case(FakeProposalRepository()).execute(
                WithdrawProposalCommand(new_proposal_id())
            )

    async def test_a_postponed_proposal_is_not_withdrawable_directly(self) -> None:
        proposal = make_proposal()
        proposal.postpone(NOW.replace(hour=23), NOW)
        proposals = FakeProposalRepository()
        await proposals.save(proposal)

        with pytest.raises(ProposalNotWithdrawableError):
            await _use_case(proposals).execute(WithdrawProposalCommand(proposal.proposal_id))

    async def test_an_already_executed_proposal_raises(self) -> None:
        proposal = make_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)
        proposal.schedule_execution(0, NOW)
        proposal.begin_execution(NOW)
        proposal.record_execution(success=True, now=NOW)
        proposals = FakeProposalRepository()
        await proposals.save(proposal)

        with pytest.raises(ProposalNotWithdrawableError):
            await _use_case(proposals).execute(WithdrawProposalCommand(proposal.proposal_id))
