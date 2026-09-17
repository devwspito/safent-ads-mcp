"""`AuthorizationKind.PACKAGE_STEP` (`003-paquete-de-campana` data-model.md
Revision 2 §R2.9/contracts/api.md §R2.E). La autorizacion que firma
`chokepoint_step_executor` para UN paso de publicacion, encadenada a la
`human_approval` de sujeto `package` via `derived_from_authorization_id` y
acompanada del sobre integro (`PackageApprovalProof`) para que el broker
verifique la firma humana sin volver a preguntarle nada a `ads-api`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationId,
    AuthorizationKind,
    AuthorizationSubjectInvariantError,
    PackageApprovalProof,
    PackageStepAuthorizationInvariantError,
    sign_authorization,
)
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.testing.fakes import FakeSignerPort, FakeVerifierPort

NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
_EXPIRES = NOW + timedelta(minutes=5)
_ENVELOPE_EXPIRES = NOW + timedelta(minutes=30)


def _proof(*, expires_at: datetime = _ENVELOPE_EXPIRES) -> PackageApprovalProof:
    return PackageApprovalProof(
        envelope={"package_id": "pkg-1", "step_plan": []},
        authorization_id=str(AuthorizationId.new()),
        issued_by="owner-1",
        expires_at=expires_at,
        signature=b"\x01\x02",
    )


def _sign_package_step(**overrides: object):
    defaults: dict[str, object] = {
        "authorization_id": AuthorizationId.new(),
        "proposal_id": ProposalId.new(),
        "kind": AuthorizationKind.PACKAGE_STEP,
        "diff_hash": "a" * 64,
        "guardrail_verdict_hash": "b" * 64,
        "issued_by": "ads-worker",
        "channel": AuthorizationChannel.RULE_ENGINE,
        "decided_at": NOW,
        "expires_at": _EXPIRES,
        "signer": FakeSignerPort(),
        "derived_from_authorization_id": AuthorizationId.new(),
        "package_approval": _proof(),
        "package_binding": {"step_index": 0, "step_kind": "CREATE_CAMPAIGN"},
    }
    defaults.update(overrides)
    return sign_authorization(**defaults)  # type: ignore[arg-type]


class TestPackageStepFirmaValida:
    def test_firma_y_verifica(self) -> None:
        authorization = _sign_package_step()

        assert FakeVerifierPort().verify(authorization.signing_payload(), authorization.signature)
        assert authorization.kind is AuthorizationKind.PACKAGE_STEP
        assert authorization.subject is None

    def test_la_carga_firmada_incluye_el_encadenado_y_el_sobre(self) -> None:
        derived_from = AuthorizationId.new()
        authorization = _sign_package_step(derived_from_authorization_id=derived_from)

        payload = authorization.signing_payload()
        assert str(derived_from).encode() in payload
        assert b"package_approval" in payload
        assert b"package_binding" in payload


class TestPackageStepExigeLosCuatroCampos:
    def test_sin_derived_from_authorization_id_no_se_firma(self) -> None:
        with pytest.raises(PackageStepAuthorizationInvariantError):
            _sign_package_step(derived_from_authorization_id=None)

    def test_sin_package_approval_no_se_firma(self) -> None:
        with pytest.raises(PackageStepAuthorizationInvariantError):
            _sign_package_step(package_approval=None)

    def test_sin_package_binding_no_se_firma(self) -> None:
        with pytest.raises(PackageStepAuthorizationInvariantError):
            _sign_package_step(package_binding=None)

    def test_sin_proposal_id_no_se_firma(self) -> None:
        # `proposal_id=None` sin `subject` dispara primero la invariante mas
        # general de la Revision 2 §R2.5 (BL-7): "exactamente uno de
        # proposal_id/subject" -- sigue siendo una denegacion, solo que con
        # el tipo de excepcion mas generico de los dos.
        with pytest.raises(AuthorizationSubjectInvariantError):
            _sign_package_step(proposal_id=None)


class TestPackageStepCamposNoSeCuelanEnOtroKind:
    def test_human_approval_con_package_binding_se_deniega(self) -> None:
        with pytest.raises(PackageStepAuthorizationInvariantError):
            sign_authorization(
                authorization_id=AuthorizationId.new(),
                proposal_id=ProposalId.new(),
                kind=AuthorizationKind.HUMAN_APPROVAL,
                diff_hash="a" * 64,
                guardrail_verdict_hash="b" * 64,
                issued_by="owner-1",
                channel=AuthorizationChannel.PANEL,
                decided_at=NOW,
                expires_at=_EXPIRES,
                signer=FakeSignerPort(),
                package_binding={"step_index": 0},
            )


class TestPackageStepSobreCaducadoAlFirmar:
    def test_sobre_caducado_en_el_instante_de_firmar_no_se_firma(self) -> None:
        with pytest.raises(PackageStepAuthorizationInvariantError):
            _sign_package_step(package_approval=_proof(expires_at=NOW))

    def test_sobre_caducado_antes_de_decided_at_no_se_firma(self) -> None:
        with pytest.raises(PackageStepAuthorizationInvariantError):
            _sign_package_step(package_approval=_proof(expires_at=NOW - timedelta(seconds=1)))
