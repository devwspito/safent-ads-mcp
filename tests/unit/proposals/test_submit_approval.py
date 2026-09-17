"""`SubmitApproval` (T076/T083, contracts/rest-api.md `POST
/proposals/{id}/approve`): panel y Telegram comparten esta pieza."""

from __future__ import annotations

import pytest

from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
    GuardrailEvaluator,
    brake_scope_from,
)
from safent_ads.execution.testing.fakes import (
    FakeBrakeStatePort,
    FakeExecutionQueuePort,
    FakeGuardrailSetRepository,
    FakeSpendLedger,
)
from safent_ads.proposals.application.submit_approval import (
    ApprovalDeniedReason,
    ProposalApprovalDeniedError,
    SubmitApproval,
    SubmitApprovalCommand,
)
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.proposals.domain.proposal import ProposalState, ProposedDiff
from safent_ads.proposals.testing.fakes import (
    FakeAuthorizationRepository,
    FakeProposalRepository,
    FakeSignerPort,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityRef
from tests.unit.execution.conftest import (
    NOW,
    empty_ledger,
    entity_scope,
    guardrail_set,
    make_pending_proposal,
)
from tests.unit.execution.test_campaign_creation_budget import creation_payload

_SCOPE = entity_scope()


def _use_case(
    proposals: FakeProposalRepository,
    *,
    brakes: FakeBrakeStatePort | None = None,
    guardrails: FakeGuardrailSetRepository | None = None,
    spend_ledger: FakeSpendLedger | None = None,
) -> tuple[SubmitApproval, FakeAuthorizationRepository, FakeExecutionQueuePort]:
    authorizations = FakeAuthorizationRepository()
    execution_queue = FakeExecutionQueuePort()

    use_case = SubmitApproval(
        proposals=proposals,
        authorizations=authorizations,
        execution_queue=execution_queue,
        brakes=brakes or FakeBrakeStatePort(),
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=guardrails or FakeGuardrailSetRepository({_SCOPE.ref: guardrail_set()}),
        spend_ledger=spend_ledger or FakeSpendLedger({_SCOPE.ref: empty_ledger()}),
        signer=FakeSignerPort(),
        clock=FixedClock(NOW),
    )
    return use_case, authorizations, execution_queue


def _command(proposal_id, diff_hash: str, **overrides: object) -> SubmitApprovalCommand:
    defaults: dict[str, object] = {
        "proposal_id": proposal_id,
        "diff_hash": diff_hash,
        "approved_by": "owner-1",
        "channel": AuthorizationChannel.PANEL,
    }
    defaults.update(overrides)
    return SubmitApprovalCommand(**defaults)  # type: ignore[arg-type]


class TestApprovesAndSchedules:
    async def test_mints_authorization_schedules_and_enqueues(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        use_case, authorizations, execution_queue = _use_case(proposals)

        result = await use_case.execute(_command(proposal.proposal_id, proposal.diff.diff_hash))

        stored = await proposals.get(proposal.proposal_id)
        assert stored is not None
        assert stored.state is ProposalState.SCHEDULED
        assert result.grace_seconds == 20  # ROUTINE
        auth = await authorizations.get_active_for_proposal(proposal.proposal_id)
        assert auth is not None
        assert auth.issued_by == "owner-1"
        enqueued = await execution_queue.get_for_proposal(proposal.proposal_id)
        assert enqueued is not None
        assert str(enqueued.execution_id) == result.execution_id


class TestDeniesOnDiffChanged:
    async def test_stale_diff_hash_is_rejected(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        use_case, _authorizations, _queue = _use_case(proposals)

        with pytest.raises(ProposalApprovalDeniedError) as excinfo:
            await use_case.execute(_command(proposal.proposal_id, "not-the-live-hash"))

        assert excinfo.value.reason is ApprovalDeniedReason.DIFF_CHANGED


class TestDeniesWhenNotPending:
    async def test_already_approved_proposal_is_rejected(self) -> None:
        proposal = make_pending_proposal()
        proposal.approve(proposal.diff.diff_hash, NOW)
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        use_case, _authorizations, _queue = _use_case(proposals)

        with pytest.raises(ProposalApprovalDeniedError) as excinfo:
            await use_case.execute(_command(proposal.proposal_id, proposal.diff.diff_hash))

        assert excinfo.value.reason is ApprovalDeniedReason.PROPOSAL_NOT_PENDING


class TestDeniesWhenBrakeEngagedInAllMode:
    async def test_all_mode_brake_blocks_human_approval(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        brakes = FakeBrakeStatePort()
        brake = EmergencyBrake(scope=brake_scope_from(_SCOPE), mode=BrakeMode.ALL)
        brake.engage("incidente", NOW)
        await brakes.save(brake)
        use_case, _authorizations, _queue = _use_case(proposals, brakes=brakes)

        with pytest.raises(ProposalApprovalDeniedError) as excinfo:
            await use_case.execute(_command(proposal.proposal_id, proposal.diff.diff_hash))

        assert excinfo.value.reason is ApprovalDeniedReason.BRAKE_ENGAGED

    async def test_autonomous_mode_brake_does_not_block_human_approval(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        brakes = FakeBrakeStatePort()
        brake = EmergencyBrake(scope=brake_scope_from(_SCOPE), mode=BrakeMode.AUTONOMOUS)
        brake.engage("degradacion", NOW)
        await brakes.save(brake)
        use_case, _authorizations, _queue = _use_case(proposals, brakes=brakes)

        result = await use_case.execute(_command(proposal.proposal_id, proposal.diff.diff_hash))

        assert result.execution_id

    async def test_global_brake_in_all_mode_blocks_human_approval(self) -> None:
        """BUG corregido: solo se comprobaba el freno de la cuenta -- uno
        GLOBAL activo dejaba pasar la aprobacion humana igualmente."""
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        brakes = FakeBrakeStatePort()
        brake = EmergencyBrake(scope=BrakeScope(kind=BrakeScopeKind.GLOBAL), mode=BrakeMode.ALL)
        brake.engage("parada general", NOW)
        await brakes.save(brake)
        use_case, _authorizations, _queue = _use_case(proposals, brakes=brakes)

        with pytest.raises(ProposalApprovalDeniedError) as excinfo:
            await use_case.execute(_command(proposal.proposal_id, proposal.diff.diff_hash))

        assert excinfo.value.reason is ApprovalDeniedReason.BRAKE_ENGAGED

    async def test_business_brake_in_all_mode_blocks_human_approval(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        brakes = FakeBrakeStatePort()
        brakes.link_account_to_business(_SCOPE.ref, "negocio-1")
        brake = EmergencyBrake(
            scope=BrakeScope(kind=BrakeScopeKind.BUSINESS, ref="negocio-1"), mode=BrakeMode.ALL
        )
        brake.engage("gasto disparado en el negocio", NOW)
        await brakes.save(brake)
        use_case, _authorizations, _queue = _use_case(proposals, brakes=brakes)

        with pytest.raises(ProposalApprovalDeniedError) as excinfo:
            await use_case.execute(_command(proposal.proposal_id, proposal.diff.diff_hash))

        assert excinfo.value.reason is ApprovalDeniedReason.BRAKE_ENGAGED

    async def test_another_businesss_brake_does_not_block_human_approval(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        brakes = FakeBrakeStatePort()
        brakes.link_account_to_business(_SCOPE.ref, "negocio-1")
        brake = EmergencyBrake(
            scope=BrakeScope(kind=BrakeScopeKind.BUSINESS, ref="otro-negocio"), mode=BrakeMode.ALL
        )
        brake.engage("incidente de otro negocio", NOW)
        await brakes.save(brake)
        use_case, _authorizations, _queue = _use_case(proposals, brakes=brakes)

        result = await use_case.execute(_command(proposal.proposal_id, proposal.diff.diff_hash))

        assert result.execution_id


class TestDeniesOnGuardrailBlocked:
    async def test_max_changes_reached_denies(self) -> None:
        proposal = make_pending_proposal()
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        use_case, authorizations, _queue = _use_case(
            proposals,
            guardrails=FakeGuardrailSetRepository(
                {_SCOPE.ref: guardrail_set(max_changes_per_entity_day=2)}
            ),
            spend_ledger=FakeSpendLedger({_SCOPE.ref: empty_ledger(changes_today=2)}),
        )

        with pytest.raises(ProposalApprovalDeniedError) as excinfo:
            await use_case.execute(_command(proposal.proposal_id, proposal.diff.diff_hash))

        assert excinfo.value.reason is ApprovalDeniedReason.GUARDRAIL_BLOCKED
        assert await authorizations.get_active_for_proposal(proposal.proposal_id) is None


def _display_creation_payload() -> dict[str, object]:
    """A smuggled `new_campaign:` plan: DISPLAY is one of the four valid
    `GoogleAdvertisingChannelType` rows (passes `creation_budget`'s own
    schema check), but this installation's default `ADS_GOOGLE_CHANNELS_
    ENABLED` never turned it on."""
    payload = creation_payload()
    payload["creation_plan"]["native"] = {
        "advertising_channel_type": "DISPLAY",
        "bidding_strategy": {"kind": "MANUAL_CPC"},
        "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    }
    return payload


class TestDeniesWhenChannelNotEnabled:
    """T035 security re-check (2026-09-15, CWE-284): defence in depth --
    `proposals.presentation.rest._edited_value` already rejects this PATCH,
    but a `new_campaign:` proposal could reach `SubmitApproval` through
    another writer of `proposal.diff.after` (or a channel this installation
    once allowed and later narrowed, `opportunities.application.propose_
    campaign.ProposeCampaign` docstring), so the gate is repeated here,
    before the brake/guardrail checks and before any authorization is
    signed."""

    async def test_a_display_plan_is_denied_under_the_search_only_default(self) -> None:
        ref = EntityRef.parse("google:account:123")
        diff = ProposedDiff.build(
            entity_ref=ref,
            parameter="new_campaign:test",
            before=None,
            after=_display_creation_payload(),
        )
        proposal = make_pending_proposal(diff=diff)
        proposals = FakeProposalRepository()
        await proposals.save(proposal)
        use_case, authorizations, queue = _use_case(proposals)

        with pytest.raises(ProposalApprovalDeniedError) as excinfo:
            await use_case.execute(_command(proposal.proposal_id, proposal.diff.diff_hash))

        assert excinfo.value.reason is ApprovalDeniedReason.CHANNEL_TYPE_NOT_ENABLED
        stored = await proposals.get(proposal.proposal_id)
        assert stored is not None
        assert stored.state is ProposalState.PENDING  # nunca se programa
        assert await authorizations.get_active_for_proposal(proposal.proposal_id) is None
        assert await queue.get_for_proposal(proposal.proposal_id) is None

    async def test_a_search_plan_never_reaches_the_channel_check(self) -> None:
        """The gate is a no-op for the channel this installation always
        enables -- pinned directly against the private helper so this test
        does not need to wire a full guardrail/ledger fixture for an
        account scope it does not otherwise use (see `TestApprovesAndSchedules`
        for the end-to-end happy path with a non-creation diff)."""
        ref = EntityRef.parse("google:account:123")
        diff = ProposedDiff.build(
            entity_ref=ref, parameter="new_campaign:test", before=None, after=creation_payload()
        )
        proposal = make_pending_proposal(diff=diff)
        use_case, _authorizations, _queue = _use_case(FakeProposalRepository())

        use_case._require_enabled_google_channel(proposal)  # noqa: SLF001 - pins the private gate directly
