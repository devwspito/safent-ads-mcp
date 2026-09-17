"""`AuthorizationSubject` (003-paquete-de-campana data-model.md Revision 2
§R2.5, BL-7; tasks.md T104). Cambio aditivo sobre `proposals.domain.
authorization`: una autorizacion de paquete nunca mete su `package_id` en
`proposal_id`, y las firmas `human_approval`/`rule_authorization` de
propuesta existentes siguen verificando byte a byte igual que antes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationDecision,
    AuthorizationId,
    AuthorizationKind,
    AuthorizationSignatureInvalidError,
    AuthorizationSubject,
    AuthorizationSubjectInvariantError,
    AuthorizationVerifier,
    SubjectKind,
    sign_authorization,
)
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.testing.fakes import FakeSignerPort, FakeVerifierPort
from safent_ads.shared.clock import FixedClock

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
_EXPIRES = NOW + timedelta(minutes=30)


def _package_authorization(
    *,
    authorization_id: AuthorizationId | None = None,
    signer: FakeSignerPort | None = None,
    package_id: str = "01J8Z9K7Q3R5T6V8W0X2Y4Z6A8",
):
    return sign_authorization(
        authorization_id=authorization_id or AuthorizationId.new(),
        subject=AuthorizationSubject(kind=SubjectKind.PACKAGE, id=package_id),
        kind=AuthorizationKind.HUMAN_APPROVAL,
        diff_hash="e" * 64,
        guardrail_verdict_hash="f" * 64,
        issued_by="owner-1",
        channel=AuthorizationChannel.PANEL,
        decided_at=NOW,
        expires_at=_EXPIRES,
        signer=signer or FakeSignerPort(),
    )


class TestFirmaDeHumanApprovalExistenteNoCambiaNiUnByte:
    def test_firma_de_human_approval_existente_no_cambia_ni_un_byte(self) -> None:
        authorization_id = AuthorizationId.new()
        proposal_id = ProposalId.new()
        authorization = sign_authorization(
            authorization_id=authorization_id,
            proposal_id=proposal_id,
            kind=AuthorizationKind.HUMAN_APPROVAL,
            diff_hash="a" * 64,
            guardrail_verdict_hash="b" * 64,
            issued_by="owner-1",
            channel=AuthorizationChannel.PANEL,
            decided_at=NOW,
            expires_at=_EXPIRES,
            signer=FakeSignerPort(),
        )

        expected_payload = canonical_json_bytes(
            {
                "authorization_id": str(authorization_id),
                "proposal_id": str(proposal_id),
                "kind": "human_approval",
                "diff_hash": "a" * 64,
                "guardrail_verdict_hash": "b" * 64,
                "issued_by": "owner-1",
                "expires_at": _EXPIRES.isoformat(),
            }
        )

        assert authorization.signing_payload() == expected_payload
        assert authorization.subject is None


class TestPackageStepSinBindingNoSeFirma:
    """Nombre del contrato (tasks.md T104); alcance real de esta entrega:
    un sujeto de paquete solo es construible para `human_approval`/`PANEL`
    -- cualquier otra combinacion se deniega en la propia `Authorization`,
    antes de que exista ningun binding que comprobar (T103, fuera de esta
    rama)."""

    def test_package_step_sin_binding_no_se_firma(self) -> None:
        with pytest.raises(AuthorizationSubjectInvariantError):
            sign_authorization(
                authorization_id=AuthorizationId.new(),
                subject=AuthorizationSubject(kind=SubjectKind.PACKAGE, id="pkg-1"),
                kind=AuthorizationKind.HUMAN_APPROVAL,
                diff_hash="a" * 64,
                guardrail_verdict_hash="b" * 64,
                issued_by="rule-engine",
                channel=AuthorizationChannel.RULE_ENGINE,
                decided_at=NOW,
                expires_at=_EXPIRES,
                signer=FakeSignerPort(),
            )

    def test_neither_proposal_id_nor_subject_is_rejected(self) -> None:
        with pytest.raises(AuthorizationSubjectInvariantError):
            sign_authorization(
                authorization_id=AuthorizationId.new(),
                kind=AuthorizationKind.HUMAN_APPROVAL,
                diff_hash="a" * 64,
                guardrail_verdict_hash="b" * 64,
                issued_by="owner-1",
                channel=AuthorizationChannel.PANEL,
                decided_at=NOW,
                expires_at=_EXPIRES,
                signer=FakeSignerPort(),
            )

    def test_both_proposal_id_and_subject_is_rejected(self) -> None:
        with pytest.raises(AuthorizationSubjectInvariantError):
            sign_authorization(
                authorization_id=AuthorizationId.new(),
                proposal_id=ProposalId.new(),
                subject=AuthorizationSubject(kind=SubjectKind.PACKAGE, id="pkg-1"),
                kind=AuthorizationKind.HUMAN_APPROVAL,
                diff_hash="a" * 64,
                guardrail_verdict_hash="b" * 64,
                issued_by="owner-1",
                channel=AuthorizationChannel.PANEL,
                decided_at=NOW,
                expires_at=_EXPIRES,
                signer=FakeSignerPort(),
            )


class TestUnaAutorizacionDePaqueteNoVerificaComoAutorizacionDePropuesta:
    """INV-14 (data-model.md Revision 2 §R2.5): `AuthorizationVerifier` es
    el verificador de `Proposal` -- resuelve `live_diff_hash` a partir de
    `proposal.diff`, algo que no existe para un sujeto de paquete
    (`proposal_id is None`). Aqui se prueba la mitad que SI vive en esta
    rama: `subject` viaja en la carga firmada (`signing_payload()`), asi
    que reverificar con la huella EQUIVOCADA (la que tendria si algo
    intentase tratarlo como propuesta, sin `subject` en la carga) falla
    por firma invalida -- `verify_package_approval` (T113, fuera de esta
    rama) es el unico camino real, y esta prueba deja constancia de por
    que un verificador de propuesta no sirve para esto."""

    def test_reverificar_con_una_carga_sin_subject_invalida_la_firma(self) -> None:
        clock = FixedClock(NOW)
        verifier = AuthorizationVerifier(FakeVerifierPort(), clock)
        package_authorization = _package_authorization()
        # La carga que un `AuthorizationVerifier` de propuesta reconstruiria
        # NO llevaria `subject` (no lo conoce) -- firmar esa carga distinta
        # y comparar contra la firma real de la autorizacion de paquete es
        # justo lo que demuestra que las dos formas no son intercambiables.
        payload_without_subject = canonical_json_bytes(
            {
                "authorization_id": str(package_authorization.authorization_id),
                "proposal_id": None,
                "kind": package_authorization.kind.value,
                "diff_hash": package_authorization.diff_hash,
                "guardrail_verdict_hash": package_authorization.guardrail_verdict_hash,
                "issued_by": package_authorization.issued_by,
                "expires_at": package_authorization.expires_at.isoformat(),
            }
        )

        assert payload_without_subject != package_authorization.signing_payload()
        assert not FakeVerifierPort().verify(
            payload_without_subject, package_authorization.signature
        )
        with pytest.raises(AuthorizationSignatureInvalidError):
            verifier.verify(
                replace(package_authorization, subject=None, proposal_id=ProposalId.new()),
                live_diff_hash=package_authorization.diff_hash,
                live_guardrail_verdict_hash=package_authorization.guardrail_verdict_hash,
            )

    def test_package_authorization_signature_verifies_on_its_own_payload(self) -> None:
        verifier = FakeVerifierPort()
        authorization = _package_authorization()

        assert verifier.verify(authorization.signing_payload(), authorization.signature)
        assert authorization.decision is AuthorizationDecision.APPROVED
        assert authorization.proposal_id is None
        assert authorization.subject == AuthorizationSubject(
            kind=SubjectKind.PACKAGE, id="01J8Z9K7Q3R5T6V8W0X2Y4Z6A8"
        )
