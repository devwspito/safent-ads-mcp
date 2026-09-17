"""Real Ed25519 + wire + durable SQLite receipt, no provider or live EE."""

import base64
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from safent_ads.accounts.application.ports import WriteIntent, WriteOperation, WriteOutcome
from safent_ads.accounts.infrastructure.broker_client import _serialize_authorization
from safent_ads.broker.domain.write_authorization import (
    authorization_signing_payload,
    recompute_diff_hash,
)
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.presentation.request_schemas import ExecuteWriteRequest
from safent_ads.composition.signing import build_approval_key_pair
from safent_ads.execution.infrastructure.broker_platform import _signed
from safent_ads.opportunities.infrastructure.sql_repositories import (
    SqlCampaignProposalPort,
    _brief_payload,
)
from safent_ads.proposals.application.submit_approval import ProposalApprovalDeniedError
from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationKind,
    RuleAuthorizationNotEligibleError,
    sign_authorization,
)
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.proposals.domain.proposal import ProposalInvariantError, ProposedDiff
from safent_ads.proposals.testing.fakes import FakeProposalRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef
from tests.unit.broker.ledger_scope_fakes import fake_scope
from tests.unit.broker.platforms.test_write_pipeline import _NOW, _pipeline
from tests.unit.execution.conftest import make_pending_proposal
from tests.unit.opportunities.test_campaign_brief import _brief
from tests.unit.proposals.test_managed_binding import binding, entity
from tests.unit.proposals.test_submit_approval import _command, _use_case


def signed(*, managed=True, kind=AuthorizationKind.HUMAN_APPROVAL):
    seed = base64.b64encode(b"m" * 32).decode()
    b = binding() if managed else None
    diff = ProposedDiff.build(entity(), "status", "ACTIVE", "PAUSED", managed_binding=b)
    authorization = sign_authorization(
        authorization_id=AuthorizationId.new(),
        proposal_id=ProposalId.new(),
        kind=kind,
        proposal_classification=Classification.ROUTINE,
        diff_hash=diff.diff_hash,
        guardrail_verdict_hash="a" * 64,
        issued_by="human-test",
        channel=AuthorizationChannel.PANEL,
        decided_at=_NOW,
        expires_at=_NOW + timedelta(minutes=15),
        signer=build_approval_key_pair(seed).signer,
        managed_binding=b,
    )
    intent = WriteIntent(
        entity(),
        WriteOperation.PAUSE,
        "status",
        "ACTIVE",
        "PAUSED",
        diff.diff_hash,
        "b" * 64,
        str(binding().account.business_id),
        b,
    )
    verifier = ApprovalVerifier.from_public_key_b64(
        ApprovalSigner.from_seed_b64(seed).public_key_b64()
    )
    return intent, authorization, verifier


def test_managed_signature_broker_parity_and_no_unsigned_context_substitution():
    intent, auth, verifier = signed()
    wire = _signed(auth)
    assert recompute_diff_hash(intent) == auth.diff_hash
    assert verifier.verify(authorization_signing_payload(wire), auth.signature)
    for modified in (None, replace(binding(), revision=2)):
        assert not verifier.verify(
            authorization_signing_payload(replace(wire, managed_binding=modified)), auth.signature
        )
    with pytest.raises(RuleAuthorizationNotEligibleError):
        signed(kind=AuthorizationKind.RULE_AUTHORIZATION)
    _, legacy, _ = signed(managed=False)
    assert "managed_binding" not in _serialize_authorization(_signed(legacy))
    assert b"managed_binding" not in legacy.signing_payload()


def test_wire_closed_binding_roundtrips_and_rejects_role_revision_or_extra_fields():
    intent, auth, _ = signed()
    request = dict(
        op="execute_write",
        entity_ref=str(intent.entity_ref),
        operation="PAUSE",
        parametro="status",
        valor_actual="ACTIVE",
        valor_propuesto="PAUSED",
        diff_hash=intent.diff_hash,
        expected_state_hash=intent.expected_state_hash,
        business_id=intent.business_id,
        managed_binding=binding().as_claims(),
        authorization=_serialize_authorization(_signed(auth)),
        idempotency_key="test-only",
    )
    parsed = ExecuteWriteRequest.model_validate(request)
    assert parsed.managed_binding == parsed.authorization.managed_binding == binding().as_claims()
    for change in ({"revision": True}, {"role": "owner"}, {"secret": "must-not-be-accepted"}):
        with pytest.raises(ValidationError):
            ExecuteWriteRequest.model_validate(
                {**request, "managed_binding": {**binding().as_claims(), **change}}
            )


def test_valid_managed_signature_cannot_write_until_fresh_admission_is_wired(tmp_path):
    intent, auth, verifier = signed()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    outcome = pipeline.begin_write("managed-new", intent, _signed(auth), "1234567890", _NOW)
    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "managed_admission_unavailable"
    assert pipeline.read_receipt("managed-new", intent, _signed(auth)) is None


def test_unknown_receipt_survives_restart_expired_authorization_and_no_live_admission(tmp_path):
    intent, auth, verifier = signed()
    ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
    scope = fake_scope(intent, "1234567890")
    ledger.begin_receipt("managed-original", intent, str(auth.authorization_id), scope, 1234)
    ledger.record_outcome_if_absent(
        "managed-original", WriteOutcome("UNKNOWN", None, None, "timeout", None)
    )
    # Independent store/pipeline reproduces restart. Past receipt needs original
    # signature, not a refreshed grant or another mutation authorization.
    restarted = _pipeline(tmp_path, verifier=verifier)
    assert restarted.read_receipt("managed-original", intent, _signed(auth)).outcome == "UNKNOWN"
    assert ledger.pending_totals(scope) == (1, 1234)
    for modified in (None, replace(binding(), revision=2)):
        with pytest.raises(ValueError):
            restarted.read_receipt(
                "managed-original", replace(intent, managed_binding=modified), _signed(auth)
            )
    assert ledger.pending_totals(scope) == (1, 1234)


async def test_local_owner_approval_cannot_mint_managed_permission():
    proposal = make_pending_proposal()
    proposal.diff = ProposedDiff.build(
        entity(), "status", "ACTIVE", "PAUSED", managed_binding=binding()
    )
    repo = FakeProposalRepository()
    await repo.save(proposal)
    case, authorizations, queue = _use_case(repo)
    with pytest.raises(ProposalApprovalDeniedError, match="MANAGED_HUMAN_ADMISSION_REQUIRED"):
        await case.execute(_command(proposal.proposal_id, proposal.diff.diff_hash))
    assert await authorizations.get_active_for_proposal(proposal.proposal_id) is None
    assert await queue.get_for_proposal(proposal.proposal_id) is None


@pytest.mark.parametrize("physical_coverage", [True, False])
async def test_opportunity_dedup_does_not_disclose_managed_proposal(physical_coverage):
    b = binding()
    account = b.account
    ref = EntityRef(
        account.platform,
        EntityLevel.ACCOUNT,
        account.external_account_id,
        account.business_id,
        account.connection_id,
    )
    brief = _brief(platform=account.platform)
    proposal = make_pending_proposal()
    proposal.diff = ProposedDiff.build(
        ref, "new_campaign:test", None, _brief_payload(brief), managed_binding=b
    )
    port = SqlCampaignProposalPort(None, clock=FixedClock(_NOW))
    port._physical_coverage = AsyncMock(return_value=proposal if physical_coverage else None)
    port._proposals = AsyncMock()
    port._proposals.find_live_equivalent.return_value = proposal
    with pytest.raises(ProposalInvariantError, match="physical_opportunity_conflict"):
        await port.accept(
            business_id=BusinessId(account.business_id),
            account_ref=ref,
            candidate_key="test",
            brief=brief,
            expected_contribution_delta=None,
            cause_sentence="test",
            now=_NOW,
        )
    port._proposals.save.assert_not_awaited()
