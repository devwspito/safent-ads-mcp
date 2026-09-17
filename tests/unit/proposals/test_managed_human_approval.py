"""An account grant is not consent; consume exact Enterprise human proof once."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from safent_ads.execution.testing.fakes import FakeGuardrailSetRepository
from safent_ads.iam.application.managed_ads_authority import (
    ManagedAdsAdmission,
    ManagedAdsDenied,
    ManagedAdsUnavailable,
)
from safent_ads.iam.application.managed_human_approval import (
    ManagedApprovalIntent,
    VerifiedManagedApproval,
)
from safent_ads.proposals.application.prepare_managed_approval import PrepareManagedApproval
from safent_ads.proposals.application.submit_approval import (
    ApprovalDeniedReason,
    ProposalApprovalDeniedError,
    SubmitApprovalCommand,
)
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.proposals.testing.fakes import FakeProposalRepository
from safent_ads.shared.clock import FixedClock
from tests.unit.execution.conftest import NOW, guardrail_set, make_pending_proposal
from tests.unit.proposals.test_managed_binding import binding, entity
from tests.unit.proposals.test_submit_approval import _use_case


async def fixture():
    b = binding()
    diff = ProposedDiff.build(
        entity(), "daily_budget", Money.of("100"), Money.of("90"), managed_binding=b
    )
    proposal = make_pending_proposal(diff=diff)
    repo = FakeProposalRepository()
    await repo.save(proposal)
    uc, auths, queue = _use_case(
        repo, guardrails=FakeGuardrailSetRepository({str(entity()): guardrail_set()})
    )
    proof = VerifiedManagedApproval(
        b,
        b.user_id,
        proposal.proposal_id.value,
        diff.diff_hash,
        uuid4(),
        int(NOW.timestamp()) + 120,
    )
    authority = AsyncMock()
    authority.consume.return_value = proof
    uc._human_authority = authority
    cmd = SubmitApprovalCommand(
        proposal.proposal_id,
        diff.diff_hash,
        "untrusted-local-owner",
        AuthorizationChannel.PANEL,
        "untrusted comment",
        "synthetic-human-proof",
    )
    return uc, proposal, repo, auths, queue, authority, cmd, proof


async def test_verified_human_identity_overrides_local_owner_and_comment():
    uc, p, _, auths, queue, authority, cmd, proof = await fixture()
    result = await uc.execute(cmd)
    saved = await auths.get_active_for_proposal(p.proposal_id)
    assert saved.issued_by == str(proof.user_id)
    assert saved.managed_binding == proof.binding
    assert saved.comment is None
    assert result.grace_seconds >= 20
    assert await queue.get_for_proposal(p.proposal_id)
    authority.consume.assert_awaited_once_with(
        cmd.human_assertion,
        binding=proof.binding,
        proposal_id=p.proposal_id.value,
        diff_hash=p.diff.diff_hash,
    )
    assert cmd.human_assertion not in repr(cmd)
    with pytest.raises(ProposalApprovalDeniedError):
        await uc.execute(cmd)
    assert authority.consume.await_count == 1


@pytest.mark.parametrize("change", ["no_port", "no_proof", "telegram", "unmanaged"])
async def test_no_owner_or_channel_downgrade(change):
    uc, p, _, auths, queue, authority, cmd, _ = await fixture()
    if change == "no_port":
        uc._human_authority = None
    elif change == "no_proof":
        cmd = replace(cmd, human_assertion=None)
    elif change == "telegram":
        cmd = replace(cmd, channel=AuthorizationChannel.TELEGRAM)
    else:
        p.diff = ProposedDiff.build(entity(), "daily_budget", Money.of("100"), Money.of("90"))
    with pytest.raises(ProposalApprovalDeniedError) as exc:
        await uc.execute(cmd)
    assert exc.value.reason == ApprovalDeniedReason.MANAGED_HUMAN_ADMISSION_REQUIRED
    assert await auths.get_active_for_proposal(p.proposal_id) is None
    assert await queue.get_for_proposal(p.proposal_id) is None
    authority.consume.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    ["user_id", "proposal_id", "diff_hash", "binding", "expires_at", "denied", "unavailable"],
)
async def test_invalid_proof_never_signs_or_queues(change):
    uc, p, _, auths, queue, authority, cmd, proof = await fixture()
    if change in {"denied", "unavailable"}:
        authority.consume.side_effect = (
            ManagedAdsDenied if change == "denied" else ManagedAdsUnavailable
        )("sensitive-upstream-content")
    else:
        wrong = {
            "user_id": uuid4(),
            "proposal_id": uuid4(),
            "diff_hash": "0" * 64,
            "binding": replace(binding(), revision=2),
            "expires_at": int(NOW.timestamp()),
        }[change]
        authority.consume.return_value = replace(proof, **{change: wrong})
    with pytest.raises(ProposalApprovalDeniedError) as exc:
        await uc.execute(cmd)
    assert "sensitive-upstream-content" not in str(exc.value)
    assert await auths.get_active_for_proposal(p.proposal_id) is None
    assert await queue.get_for_proposal(p.proposal_id) is None


@pytest.mark.parametrize("change", ["payload", "binding", "expiry", "clamp"])
async def test_state_changes_during_consumption_burn_proof_without_signing(change):
    uc, p, _, auths, queue, authority, cmd, proof = await fixture()

    async def consume(*args, **kwargs):
        if change == "payload":
            p.diff = p.diff.with_new_value(Money.of("80"))
        elif change == "binding":
            p.diff = ProposedDiff.build(
                entity(),
                "daily_budget",
                Money.of("100"),
                Money.of("90"),
                managed_binding=replace(binding(), revision=2),
            )
        elif change == "expiry":
            uc._clock = FixedClock(NOW + timedelta(seconds=121))
        else:
            uc._guardrail_sets = FakeGuardrailSetRepository(
                {str(entity()): guardrail_set(max_step_pct=0.01)}
            )
        return proof

    authority.consume.side_effect = consume
    with pytest.raises(ProposalApprovalDeniedError):
        await uc.execute(cmd)
    assert await auths.get_active_for_proposal(p.proposal_id) is None
    assert await queue.get_for_proposal(p.proposal_id) is None


async def test_prepare_uses_stored_exact_snapshot_not_browser_payload():
    _, p, repo, _, _, _, _, proof = await fixture()
    registrar = AsyncMock()
    registrar.register.return_value = ManagedApprovalIntent(
        uuid4(), p.proposal_id.value, "2026-09-09T10:15:00+00:00"
    )
    prepare = PrepareManagedApproval(repo, registrar, FixedClock(NOW))
    admission = ManagedAdsAdmission(proof.binding, int(NOW.timestamp()) + 100, "approve")
    await prepare.execute(p.proposal_id, admission)
    snapshot = registrar.register.call_args.args[0]
    assert snapshot["proposal_id"] == str(p.proposal_id)
    assert snapshot["diff_hash"] == p.diff.diff_hash
    assert snapshot["diff"]["managed_binding"] == binding().as_claims()
    for invalid in (
        replace(admission, operation="read"),
        replace(admission, expires_at=0),
        replace(admission, binding=replace(binding(), user_id=uuid4())),
    ):
        with pytest.raises(ManagedAdsDenied):
            await prepare.execute(p.proposal_id, invalid)
    assert registrar.register.await_count == 1
