"""`Authorization` + `AuthorizationVerifier` (T058)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationDecision,
    AuthorizationDiffMismatchError,
    AuthorizationExpiredError,
    AuthorizationGuardrailMismatchError,
    AuthorizationKind,
    AuthorizationNotApprovedError,
    AuthorizationSignatureInvalidError,
    AuthorizationVerifier,
    RuleAuthorizationNotEligibleError,
    sign_authorization,
)
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import AuthorizationId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.testing.fakes import FakeSignerPort, FakeVerifierPort
from safent_ads.shared.clock import FixedClock

from .conftest import NOW, make_proposal


def _sign(
    *,
    proposal_id=None,
    diff_hash: str = "diff-hash-abc",
    verdict_hash: str = "verdict-hash-abc",
    expires_at=None,
    signer: FakeSignerPort | None = None,
    kind: AuthorizationKind = AuthorizationKind.HUMAN_APPROVAL,
    classification: Classification = Classification.ROUTINE,
):
    proposal = make_proposal()
    return sign_authorization(
        authorization_id=AuthorizationId.new(),
        proposal_id=proposal_id or proposal.proposal_id,
        kind=kind,
        proposal_classification=classification,
        diff_hash=diff_hash,
        guardrail_verdict_hash=verdict_hash,
        issued_by="owner-1",
        channel=AuthorizationChannel.PANEL,
        decided_at=NOW,
        expires_at=expires_at or NOW + timedelta(hours=1),
        signer=signer or FakeSignerPort(),
    )


class TestSignAuthorization:
    def test_signature_is_non_empty_and_verifiable(self) -> None:
        signer = FakeSignerPort()
        verifier = FakeVerifierPort()
        authorization = _sign(signer=signer)

        assert authorization.signature
        assert verifier.verify(authorization.signing_payload(), authorization.signature)

    def test_decision_defaults_to_approved(self) -> None:
        authorization = _sign()

        assert authorization.decision is AuthorizationDecision.APPROVED


class TestApprovalSignatureDiffBinding:
    def test_approval_signature_diff_binding(self) -> None:
        clock = FixedClock(NOW)
        verifier = AuthorizationVerifier(FakeVerifierPort(), clock)
        authorization = _sign(diff_hash="hash-A", verdict_hash="verdict-A")

        verifier.verify(
            authorization, live_diff_hash="hash-A", live_guardrail_verdict_hash="verdict-A"
        )

    def test_diff_hash_mismatch_is_rejected(self) -> None:
        clock = FixedClock(NOW)
        verifier = AuthorizationVerifier(FakeVerifierPort(), clock)
        authorization = _sign(diff_hash="hash-A", verdict_hash="verdict-A")

        with pytest.raises(AuthorizationDiffMismatchError):
            verifier.verify(
                authorization, live_diff_hash="hash-B", live_guardrail_verdict_hash="verdict-A"
            )

    def test_guardrail_verdict_hash_mismatch_is_rejected(self) -> None:
        clock = FixedClock(NOW)
        verifier = AuthorizationVerifier(FakeVerifierPort(), clock)
        authorization = _sign(diff_hash="hash-A", verdict_hash="verdict-A")

        with pytest.raises(AuthorizationGuardrailMismatchError):
            verifier.verify(
                authorization, live_diff_hash="hash-A", live_guardrail_verdict_hash="verdict-B"
            )

    def test_tampered_signature_is_rejected(self) -> None:
        clock = FixedClock(NOW)
        verifier = AuthorizationVerifier(FakeVerifierPort(key=b"other-key"), clock)
        authorization = _sign(diff_hash="hash-A", verdict_hash="verdict-A")

        with pytest.raises(AuthorizationSignatureInvalidError):
            verifier.verify(
                authorization, live_diff_hash="hash-A", live_guardrail_verdict_hash="verdict-A"
            )


class TestExpiredAuthorizationRejected:
    def test_expired_authorization_rejected(self) -> None:
        authorization = _sign(expires_at=NOW + timedelta(minutes=5))
        after_expiry = FixedClock(NOW + timedelta(minutes=6))
        verifier = AuthorizationVerifier(FakeVerifierPort(), after_expiry)

        with pytest.raises(AuthorizationExpiredError):
            verifier.verify(
                authorization,
                live_diff_hash=authorization.diff_hash,
                live_guardrail_verdict_hash=authorization.guardrail_verdict_hash,
            )

    def test_not_yet_expired_passes(self) -> None:
        authorization = _sign(expires_at=NOW + timedelta(minutes=5))
        before_expiry = FixedClock(NOW + timedelta(minutes=4))
        verifier = AuthorizationVerifier(FakeVerifierPort(), before_expiry)

        verifier.verify(
            authorization,
            live_diff_hash=authorization.diff_hash,
            live_guardrail_verdict_hash=authorization.guardrail_verdict_hash,
        )


class TestEditedProposalInvalidatesAuthorization:
    def test_edited_proposal_invalidates_authorization(self) -> None:
        """La verificacion recalcula el `diff_hash` desde la propuesta viva:
        si `edit_proposed_value` cambio el diff, una autorizacion emitida
        para el hash anterior deja de ser valida (invariante 2)."""
        proposal = make_proposal()
        authorization = _sign(
            proposal_id=proposal.proposal_id,
            diff_hash=proposal.diff.diff_hash,
            verdict_hash="verdict-A",
        )

        proposal.edit_proposed_value(Money.of("55"), NOW)

        verifier = AuthorizationVerifier(FakeVerifierPort(), FixedClock(NOW))
        with pytest.raises(AuthorizationDiffMismatchError):
            verifier.verify(
                authorization,
                live_diff_hash=proposal.diff.diff_hash,
                live_guardrail_verdict_hash="verdict-A",
            )


class TestRuleAuthorizationRequiresRoutineClassification:
    def test_rule_authorization_rejected_for_important_classification(self) -> None:
        with pytest.raises(RuleAuthorizationNotEligibleError):
            _sign(
                kind=AuthorizationKind.RULE_AUTHORIZATION,
                classification=Classification.IMPORTANT,
            )

    def test_rule_authorization_rejected_for_critical_classification(self) -> None:
        with pytest.raises(RuleAuthorizationNotEligibleError):
            _sign(kind=AuthorizationKind.RULE_AUTHORIZATION, classification=Classification.CRITICAL)

    def test_rule_authorization_allowed_for_routine_classification(self) -> None:
        authorization = _sign(
            kind=AuthorizationKind.RULE_AUTHORIZATION, classification=Classification.ROUTINE
        )

        assert authorization.kind is AuthorizationKind.RULE_AUTHORIZATION

    def test_human_approval_allowed_for_any_classification(self) -> None:
        authorization = _sign(
            kind=AuthorizationKind.HUMAN_APPROVAL, classification=Classification.CRITICAL
        )

        assert authorization.kind is AuthorizationKind.HUMAN_APPROVAL


class TestNonApprovedDecisionIsRejected:
    def test_rejected_decision_never_authorizes(self) -> None:
        authorization = replace(_sign(), decision=AuthorizationDecision.REJECTED)
        verifier = AuthorizationVerifier(FakeVerifierPort(), FixedClock(NOW))

        with pytest.raises(AuthorizationNotApprovedError):
            verifier.verify(
                authorization,
                live_diff_hash=authorization.diff_hash,
                live_guardrail_verdict_hash=authorization.guardrail_verdict_hash,
            )
