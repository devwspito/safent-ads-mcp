"""Paridad entre `Authorization.signing_payload()`
(`proposals/domain/authorization.py`, firma en `ads-api`) y el payload que
el broker reconstruye para verificar
(`broker/domain/write_authorization.py::authorization_signing_payload`).

Los dos DEBEN canonicalizar exactamente el mismo conjunto de campos --
`issued_by` incluido en los dos a proposito (identifica quien decidio,
parte del significado de la auditoria). Esta prueba firma con el material
real de `ads-api` (`composition.signing.build_approval_key_pair`) y
verifica con un `ApprovalVerifier` del broker construido de forma
independiente (misma clave publica, cero codigo compartido de por medio,
igual que en produccion: la clave publica viaja como texto plano en
`BrokerSettings.approval_public_key`). Si un lado deja de incluir un campo
que el otro sigue firmando -- o al reves -- la verificacion falla aqui, no
en produccion."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from safent_ads.broker.domain.write_authorization import authorization_signing_payload
from safent_ads.composition.signing import build_approval_key_pair
from safent_ads.execution.infrastructure.broker_platform import _signed
from safent_ads.proposals.domain.authorization import (
    AuthorizationChannel,
    AuthorizationKind,
    sign_authorization,
)
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import AuthorizationId, ProposalId
from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_SEED_B64 = base64.b64encode(b"p" * 32).decode()


def _signed_rule_authorization() -> tuple[str, object]:
    """Firma una `Authorization` de tipo `rule_authorization` con el mismo
    material que usaria `AuthorizeRuleAction` en produccion
    (`composition.signing.build_approval_key_pair`)."""
    api_signer = build_approval_key_pair(_SEED_B64).signer
    authorization = sign_authorization(
        authorization_id=AuthorizationId.new(),
        proposal_id=ProposalId.new(),
        kind=AuthorizationKind.RULE_AUTHORIZATION,
        proposal_classification=Classification.ROUTINE,
        diff_hash="d" * 64,
        guardrail_verdict_hash="v" * 64,
        issued_by="M05",
        channel=AuthorizationChannel.RULE_ENGINE,
        decided_at=_NOW,
        expires_at=_NOW + timedelta(minutes=15),
        signer=api_signer,
    )
    public_key_b64 = ApprovalSigner.from_seed_b64(_SEED_B64).public_key_b64()
    return public_key_b64, authorization


def test_a_rule_authorization_signed_by_the_api_verifies_against_the_brokers_own_payload() -> (
    None
):
    public_key_b64, authorization = _signed_rule_authorization()
    broker_verifier = ApprovalVerifier.from_public_key_b64(public_key_b64)

    wire_authorization = _signed(authorization)

    assert broker_verifier.verify(
        authorization_signing_payload(wire_authorization),
        bytes.fromhex(wire_authorization.signature),
    )


def test_a_wire_authorization_with_a_tampered_issued_by_fails_verification() -> None:
    """`issued_by` es parte del payload firmado, no metadato de
    acompanamiento: cambiarlo despues de firmar invalida la firma, igual
    que cambiar `diff_hash` o `kind`."""
    public_key_b64, authorization = _signed_rule_authorization()
    broker_verifier = ApprovalVerifier.from_public_key_b64(public_key_b64)
    wire_authorization = _signed(authorization)

    tampered_payload = dict(authorization_signing_payload(wire_authorization))
    tampered_payload["issued_by"] = "attacker-controlled-rule"

    assert not broker_verifier.verify(
        tampered_payload, bytes.fromhex(wire_authorization.signature)
    )
