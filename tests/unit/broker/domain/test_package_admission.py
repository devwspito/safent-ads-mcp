"""`broker/domain/package_admission.py`: las siete reglas de admision de un
`package_step` (`003-paquete-de-campana` contracts/api.md §R2.E,
data-model.md Revision 2 §R2.4). Una prueba positiva por regla (tocarla
deniega con SU codigo) y una prueba de "camino feliz" que las supera todas."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from safent_ads.accounts.application.ports import (
    SignedAuthorization,
    WriteIntent,
    WriteOperation,
    WriteOutcome,
)
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.broker.domain.package_admission import (
    admit_package_step,
    admit_package_upload,
    build_package_step_idempotency_key,
)
from safent_ads.broker.domain.write_authorization import WriteDenialCode
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes, compute_diff_hash
from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_LATER = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
_BUSINESS_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_CONNECTION_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ACCOUNT_REF = EntityRef(
    PlatformCode.META, EntityLevel.ACCOUNT, "act_100", _BUSINESS_ID, _CONNECTION_ID
)
_CAMPAIGN_REF = EntityRef(
    PlatformCode.META, EntityLevel.CAMPAIGN, "act_100/c1", _BUSINESS_ID, _CONNECTION_ID
)
_PUBLICATION_ID = "pub-1"


def _keypair() -> tuple[ApprovalSigner, ApprovalVerifier]:
    seed_b64 = base64.b64encode(b"9" * 32).decode()
    signer = ApprovalSigner.from_seed_b64(seed_b64)
    return signer, ApprovalVerifier.from_public_key_b64(signer.public_key_b64())


def _campaign_creation_payload() -> dict[str, object]:
    return {"creation_plan": {"name": "Campaña de reserva"}}


def _step_plan() -> list[dict[str, object]]:
    campaign_template = _campaign_creation_payload()
    campaign_hash = hashlib.sha256(canonical_json_bytes(campaign_template)).hexdigest()
    ad_set_template = {"child_plan": {"name": "Conjunto 1"}}
    ad_set_hash = hashlib.sha256(canonical_json_bytes(ad_set_template)).hexdigest()
    activate_template = {"status": "ACTIVE"}
    activate_hash = hashlib.sha256(canonical_json_bytes(activate_template)).hexdigest()
    return [
        {
            "step_index": 0,
            "step_kind": "CREATE_CAMPAIGN",
            "local_ref": "campaign",
            "parent_local_ref": None,
            "payload_template_hash": campaign_hash,
            "expected_done_steps": None,
        },
        {
            "step_index": 1,
            "step_kind": "CREATE_AD_SET",
            "local_ref": "as#1",
            "parent_local_ref": "campaign",
            "payload_template_hash": ad_set_hash,
            "expected_done_steps": None,
        },
        {
            "step_index": 2,
            "step_kind": "ACTIVATE_CAMPAIGN",
            "local_ref": "campaign",
            "parent_local_ref": "campaign",
            "payload_template_hash": activate_hash,
            "expected_done_steps": 1,
        },
    ]


def _envelope(**overrides: object) -> dict[str, object]:
    envelope = {
        "envelope_version": 2,
        "package_id": "pkg-1",
        "package_hash": "h" * 64,
        "business_id": str(_BUSINESS_ID),
        "platform": "meta",
        "account_ref": str(_ACCOUNT_REF),
        "publication_id": _PUBLICATION_ID,
        "approved_by": "owner-1",
        "approved_at": _NOW.isoformat(),
        "approval_expires_at": _LATER.isoformat(),
        "step_plan": _step_plan(),
    }
    envelope.update(overrides)
    return envelope


def _signed_approval(
    signer: ApprovalSigner, envelope: dict[str, object], **overrides: object
) -> dict[str, object]:
    signature = signer.sign(envelope)
    approval = {
        "envelope": envelope,
        "authorization_id": "human-auth-1",
        "issued_by": envelope["approved_by"],
        "expires_at": envelope["approval_expires_at"],
        "signature": signature.hex(),
    }
    approval.update(overrides)
    return approval


def _binding(step: dict[str, object], **overrides: object) -> dict[str, object]:
    binding = {
        "package_id": "pkg-1",
        "package_hash": "h" * 64,
        "publication_id": _PUBLICATION_ID,
        "envelope_hash": "",  # filled by caller from the real envelope
        "step_index": step["step_index"],
        "step_kind": step["step_kind"],
        "local_ref": step["local_ref"],
        "parent_local_ref": step["parent_local_ref"],
        "parent_step_index": None,
        "parent_entity_ref": None,
        "account_ref": str(_ACCOUNT_REF),
        "payload_template_hash": step["payload_template_hash"],
        "creative_sources": (),
        "expected_done_steps": step["expected_done_steps"],
    }
    binding.update(overrides)
    return binding


def _envelope_hash(envelope: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()


def _authorization(
    *, kind: str = "package_step", package_approval: dict[str, object] | None, diff_hash: str
) -> SignedAuthorization:
    return SignedAuthorization(
        authorization_id="step-auth-1",
        proposal_id="proposal-1",
        kind=kind,  # type: ignore[arg-type]
        diff_hash=diff_hash,
        guardrail_verdict_hash="v" * 64,
        issued_by="ads-worker",
        expires_at=_LATER,
        signature="ab" * 32,
        package_approval=package_approval,
    )


def _campaign_intent(*, binding: dict[str, object] | None) -> WriteIntent:
    after = _campaign_creation_payload()
    diff_hash = compute_diff_hash(_ACCOUNT_REF, "new_campaign:x", None, after)
    return WriteIntent(
        entity_ref=_ACCOUNT_REF,
        operation=WriteOperation.CREATE_CAMPAIGN,
        parametro="new_campaign:x",
        valor_actual=None,
        valor_propuesto=after,
        diff_hash=diff_hash,
        expected_state_hash="",
        business_id=str(_BUSINESS_ID),
        package_binding=binding,
    )


def _fake_ledger(resources: dict[str, str] | None = None) -> object:
    table = resources or {}

    def _resolve(key: str) -> str | None:
        return table.get(key)

    return _resolve


def _campaign_diff_hash(after: dict[str, object] | None = None) -> str:
    return compute_diff_hash(
        _ACCOUNT_REF, "new_campaign:x", None, after or _campaign_creation_payload()
    )


def _happy_path() -> tuple[
    ApprovalSigner, list[dict[str, object]], dict[str, object], dict[str, object]
]:
    signer, _ = _keypair()
    step_plan = _step_plan()
    envelope = _envelope(step_plan=step_plan)
    binding = _binding(step_plan[0], envelope_hash=_envelope_hash(envelope))
    return signer, step_plan, envelope, binding


def test_campaign_creation_step_is_admitted_end_to_end() -> None:
    signer, _, envelope, binding = _happy_path()
    approval = _signed_approval(signer, envelope)
    diff_hash = _campaign_diff_hash()
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = _campaign_intent(binding=binding)
    _, verifier = _keypair()

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(),
    )

    assert denial is None


def test_non_package_step_kind_passes_through_unchanged() -> None:
    _, verifier = _keypair()
    authorization = SignedAuthorization(
        authorization_id="a1",
        proposal_id="p1",
        kind="human_approval",
        diff_hash="d" * 64,
        guardrail_verdict_hash="v" * 64,
        issued_by="owner-1",
        expires_at=_LATER,
        signature="ab" * 32,
    )
    intent = _campaign_intent(binding=None)

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(),
    )

    assert denial is None


def test_r1_binding_without_package_step_kind_is_denied() -> None:
    """R1: un `kind` que no sea `package_step` nunca lleva sobre/binding."""
    signer, verifier = _keypair()
    _, _, envelope, binding = _happy_path()
    approval = _signed_approval(signer, envelope)
    authorization = SignedAuthorization(
        authorization_id="a1",
        proposal_id="p1",
        kind="human_approval",
        diff_hash="d" * 64,
        guardrail_verdict_hash="v" * 64,
        issued_by="owner-1",
        expires_at=_LATER,
        signature="ab" * 32,
        package_approval=approval,
    )
    intent = _campaign_intent(binding=binding)

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(),
    )

    assert denial is WriteDenialCode.PACKAGE_BINDING_NOT_ALLOWED


def test_r1_package_step_without_binding_is_denied() -> None:
    signer, _, envelope, _ = _happy_path()
    approval = _signed_approval(signer, envelope)
    diff_hash = _campaign_diff_hash()
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = _campaign_intent(binding=None)
    _, verifier = _keypair()

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(),
    )

    assert denial is WriteDenialCode.PACKAGE_BINDING_REQUIRED


def test_r2_tampered_envelope_signature_is_denied() -> None:
    signer, _, envelope, binding = _happy_path()
    approval = _signed_approval(signer, envelope)
    tampered_envelope = {**envelope, "package_hash": "f" * 64}
    approval = {**approval, "envelope": tampered_envelope}
    diff_hash = _campaign_diff_hash()
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = _campaign_intent(binding=binding)
    _, verifier = _keypair()

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(),
    )

    assert denial is WriteDenialCode.PACKAGE_APPROVAL_INVALID


def test_r2_expired_human_approval_is_denied() -> None:
    signer, _, envelope, binding = _happy_path()
    approval = _signed_approval(signer, envelope)
    diff_hash = _campaign_diff_hash()
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = _campaign_intent(binding=binding)
    _, verifier = _keypair()

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=datetime(2026, 9, 16, tzinfo=UTC),  # después de approval_expires_at
        resolve_created_resource=_fake_ledger(),
    )

    assert denial is WriteDenialCode.PACKAGE_APPROVAL_EXPIRED


def test_r3_binding_package_hash_not_matching_envelope_is_denied() -> None:
    signer, _, envelope, binding = _happy_path()
    approval = _signed_approval(signer, envelope)
    diff_hash = _campaign_diff_hash()
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    tampered_binding = {**binding, "package_hash": "0" * 64}
    intent = _campaign_intent(binding=tampered_binding)
    _, verifier = _keypair()

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(),
    )

    assert denial is WriteDenialCode.PACKAGE_CHANGED


def test_r4_binding_step_index_out_of_range_is_denied() -> None:
    signer, _, envelope, binding = _happy_path()
    approval = _signed_approval(signer, envelope)
    diff_hash = _campaign_diff_hash()
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    tampered_binding = {**binding, "step_index": 99}
    intent = _campaign_intent(binding=tampered_binding)
    _, verifier = _keypair()

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(),
    )

    assert denial is WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE


def test_r4_activation_step_requires_positive_expected_done_steps() -> None:
    signer, step_plan, envelope, _ = _happy_path()
    zeroed_plan = [
        {**step, "expected_done_steps": 0} if step["step_kind"] == "ACTIVATE_CAMPAIGN" else step
        for step in step_plan
    ]
    envelope = _envelope(step_plan=zeroed_plan)
    approval = _signed_approval(signer, envelope)
    activate_step = zeroed_plan[2]
    binding = _binding(activate_step, envelope_hash=_envelope_hash(envelope), parent_step_index=0)
    diff_hash = compute_diff_hash(_CAMPAIGN_REF, "status", "PAUSED", "ACTIVE")
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = WriteIntent(
        entity_ref=_CAMPAIGN_REF,
        operation=WriteOperation.RESUME,
        parametro="status",
        valor_actual="PAUSED",
        valor_propuesto="ACTIVE",
        diff_hash=diff_hash,
        expected_state_hash="s" * 64,
        business_id=str(_BUSINESS_ID),
        package_binding=binding,
    )
    _, verifier = _keypair()
    parent_key = build_package_step_idempotency_key(_PUBLICATION_ID, 0)

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger({parent_key: "act_100/c1"}),
    )

    assert denial is WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE


def test_r5_parent_without_confirmed_receipt_is_denied() -> None:
    signer, step_plan, envelope, _ = _happy_path()
    ad_set_step = step_plan[1]
    binding = _binding(
        ad_set_step,
        envelope_hash=_envelope_hash(envelope),
        parent_step_index=0,
        parent_entity_ref=str(_CAMPAIGN_REF),
    )
    approval = _signed_approval(signer, envelope)
    after = {"child_plan": {"name": "Conjunto 1"}}
    diff_hash = compute_diff_hash(_CAMPAIGN_REF, "new_ad_set:x", None, after)
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = WriteIntent(
        entity_ref=_CAMPAIGN_REF,
        operation=WriteOperation.CREATE_AD_SET,
        parametro="new_ad_set:x",
        valor_actual=None,
        valor_propuesto=after,
        diff_hash=diff_hash,
        expected_state_hash="",
        business_id=str(_BUSINESS_ID),
        package_binding=binding,
    )
    _, verifier = _keypair()

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(),  # sin recibo del padre
    )

    assert denial is WriteDenialCode.PACKAGE_PARENT_UNCONFIRMED


def test_r5_parent_resource_mismatch_is_denied() -> None:
    signer, step_plan, envelope, _ = _happy_path()
    ad_set_step = step_plan[1]
    binding = _binding(
        ad_set_step,
        envelope_hash=_envelope_hash(envelope),
        parent_step_index=0,
        parent_entity_ref=str(_CAMPAIGN_REF),
    )
    approval = _signed_approval(signer, envelope)
    after = {"child_plan": {"name": "Conjunto 1"}}
    diff_hash = compute_diff_hash(_CAMPAIGN_REF, "new_ad_set:x", None, after)
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = WriteIntent(
        entity_ref=_CAMPAIGN_REF,
        operation=WriteOperation.CREATE_AD_SET,
        parametro="new_ad_set:x",
        valor_actual=None,
        valor_propuesto=after,
        diff_hash=diff_hash,
        expected_state_hash="",
        business_id=str(_BUSINESS_ID),
        package_binding=binding,
    )
    _, verifier = _keypair()
    key = build_package_step_idempotency_key(_PUBLICATION_ID, 0)

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger({key: "act_100/OTHER_CAMPAIGN"}),
    )

    assert denial is WriteDenialCode.PACKAGE_PARENT_UNCONFIRMED


def test_r6_scope_mismatch_denied_when_intent_business_id_differs() -> None:
    """R6 se evalua tras R5: `entity_ref` sigue igualando `account_ref` (R5
    en paz), pero el `business_id` DECLARADO del intent (un campo aparte del
    `WriteIntent`, que el bróker tambien exige igual a la cuenta del sobre)
    pertenece a otro negocio."""
    signer, _, envelope, binding = _happy_path()
    approval = _signed_approval(signer, envelope)
    after = _campaign_creation_payload()
    diff_hash = compute_diff_hash(_ACCOUNT_REF, "new_campaign:x", None, after)
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = WriteIntent(
        entity_ref=_ACCOUNT_REF,
        operation=WriteOperation.CREATE_CAMPAIGN,
        parametro="new_campaign:x",
        valor_actual=None,
        valor_propuesto=after,
        diff_hash=diff_hash,
        expected_state_hash="",
        business_id=str(uuid.uuid4()),
        package_binding=binding,
    )
    _, verifier = _keypair()

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(),
    )

    assert denial is WriteDenialCode.PACKAGE_SCOPE_MISMATCH


def _admit_with(
    binding: dict[str, object],
    envelope: dict[str, object],
    signer: ApprovalSigner,
    entity_ref: EntityRef,
    business_id: str,
) -> WriteDenialCode | None:
    approval = _signed_approval(signer, envelope)
    after = _campaign_creation_payload()
    diff_hash = compute_diff_hash(entity_ref, "new_campaign:x", None, after)
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = WriteIntent(
        entity_ref=entity_ref,
        operation=WriteOperation.CREATE_CAMPAIGN,
        parametro="new_campaign:x",
        valor_actual=None,
        valor_propuesto=after,
        diff_hash=diff_hash,
        expected_state_hash="",
        business_id=business_id,
        package_binding=binding,
    )
    _, verifier = _keypair()
    return admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(),
    )


def test_un_account_ref_ajeno_al_sobre_deniega_package_scope_mismatch() -> None:
    """H-1: un sobre íntegro de `act_100`/negocio A no puede reutilizarse con un
    binding que apunte a otra cuenta y otro negocio aunque el intent sea
    coherente con ese binding."""
    signer, _, envelope, binding = _happy_path()
    other_business, other_connection = uuid.uuid4(), uuid.uuid4()
    other_ref = EntityRef(
        PlatformCode.META, EntityLevel.ACCOUNT, "act_999", other_business, other_connection
    )
    foreign = dict(binding, account_ref=str(other_ref))

    denial = _admit_with(foreign, envelope, signer, other_ref, str(other_business))

    assert denial is WriteDenialCode.PACKAGE_SCOPE_MISMATCH


@pytest.mark.parametrize("field", ["package_id", "publication_id"])
def test_identidad_del_paquete_ajena_al_sobre_deniega(field: str) -> None:
    signer, _, envelope, binding = _happy_path()
    foreign = dict(binding, **{field: "otro"})

    denial = _admit_with(foreign, envelope, signer, _ACCOUNT_REF, str(_BUSINESS_ID))

    assert denial is WriteDenialCode.PACKAGE_SCOPE_MISMATCH


def test_r7_tampered_write_payload_is_denied() -> None:
    signer, _, envelope, binding = _happy_path()
    approval = _signed_approval(signer, envelope)
    tampered_after = {"creation_plan": {"name": "Campaña ALTERADA"}}
    diff_hash = compute_diff_hash(_ACCOUNT_REF, "new_campaign:x", None, tampered_after)
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = WriteIntent(
        entity_ref=_ACCOUNT_REF,
        operation=WriteOperation.CREATE_CAMPAIGN,
        parametro="new_campaign:x",
        valor_actual=None,
        valor_propuesto=tampered_after,
        diff_hash=diff_hash,
        expected_state_hash="",
        business_id=str(_BUSINESS_ID),
        package_binding=binding,
    )
    _, verifier = _keypair()

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(),
    )

    assert denial is WriteDenialCode.PACKAGE_PAYLOAD_NOT_REPRODUCIBLE


def test_r7_creative_source_without_confirmed_upload_is_denied() -> None:
    signer, step_plan, envelope, _ = _happy_path()
    upload_step = {
        "step_index": 0,
        "step_kind": "UPLOAD_CREATIVE",
        "local_ref": "img#abc",
        "parent_local_ref": None,
        "payload_template_hash": "u" * 64,
        "expected_done_steps": None,
    }
    ad_step_template = {"child_plan": {"picture": "{creative_of:img#abc}"}}
    ad_step_hash = hashlib.sha256(canonical_json_bytes(ad_step_template)).hexdigest()
    ad_step = {
        "step_index": 2,
        "step_kind": "CREATE_AD",
        "local_ref": "as#1/ad#1",
        "parent_local_ref": "as#1",
        "payload_template_hash": ad_step_hash,
        "expected_done_steps": None,
        "depends_on": ["img#abc"],
    }
    plan = [
        upload_step,
        {**step_plan[1], "step_index": 1},
        ad_step,
        {**step_plan[2], "step_index": 3, "parent_local_ref": "campaign", "expected_done_steps": 1},
    ]
    envelope = _envelope(step_plan=plan)
    approval = _signed_approval(signer, envelope)
    binding = _binding(
        ad_step,
        envelope_hash=_envelope_hash(envelope),
        parent_step_index=1,
        parent_entity_ref="act_100/as1",
        creative_sources=("img#abc",),
    )
    after = {"child_plan": {"picture": "https://example.com/x.jpg"}}
    ad_set_ref = EntityRef(
        PlatformCode.META, EntityLevel.AD_SET, "act_100/as1", _BUSINESS_ID, _CONNECTION_ID
    )
    diff_hash = compute_diff_hash(ad_set_ref, "new_ad:x", None, after)
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = WriteIntent(
        entity_ref=ad_set_ref,
        operation=WriteOperation.CREATE_AD,
        parametro="new_ad:x",
        valor_actual=None,
        valor_propuesto=after,
        diff_hash=diff_hash,
        expected_state_hash="",
        business_id=str(_BUSINESS_ID),
        package_binding=binding,
    )
    _, verifier = _keypair()
    parent_key = build_package_step_idempotency_key(_PUBLICATION_ID, 1)

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        # sin recibo de la imagen en el libro
        resolve_created_resource=_fake_ledger({parent_key: "act_100/as1"}),
    )

    assert denial is WriteDenialCode.PACKAGE_PAYLOAD_NOT_REPRODUCIBLE


def test_activation_payload_must_be_the_exact_status_transition() -> None:
    signer, step_plan, envelope, _ = _happy_path()
    activate_step = step_plan[2]
    binding = _binding(
        activate_step,
        envelope_hash=_envelope_hash(envelope),
        parent_step_index=0,
        parent_entity_ref=str(_CAMPAIGN_REF),
    )
    approval = _signed_approval(signer, envelope)
    diff_hash = compute_diff_hash(_CAMPAIGN_REF, "status", "PAUSED", "ACTIVE")
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    parent_key = build_package_step_idempotency_key(_PUBLICATION_ID, 0)
    intent = WriteIntent(
        entity_ref=_CAMPAIGN_REF,
        operation=WriteOperation.RESUME,
        parametro="status",
        valor_actual="PAUSED",
        valor_propuesto="ACTIVE",
        diff_hash=diff_hash,
        expected_state_hash="s" * 64,
        business_id=str(_BUSINESS_ID),
        package_binding=binding,
    )
    _, verifier = _keypair()

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger({parent_key: "act_100/c1"}),
    )

    assert denial is None


def _upload_and_ad_step_plan() -> tuple[list[dict[str, object]], dict[str, object]]:
    base_plan = _step_plan()
    upload_step = {
        "step_index": 0,
        "step_kind": "UPLOAD_CREATIVE",
        "local_ref": "img#abc",
        "parent_local_ref": None,
        "payload_template_hash": "u" * 64,
        "expected_done_steps": None,
        "depends_on": [],
    }
    ad_step_template = {"child_plan": {"picture": "{creative_of:img#abc}"}}
    ad_step_hash = hashlib.sha256(canonical_json_bytes(ad_step_template)).hexdigest()
    ad_step = {
        "step_index": 2,
        "step_kind": "CREATE_AD",
        "local_ref": "as#1/ad#1",
        "parent_local_ref": "as#1",
        "payload_template_hash": ad_step_hash,
        "expected_done_steps": None,
        "depends_on": ["img#abc"],
    }
    plan = [
        upload_step,
        {**base_plan[1], "step_index": 1},
        ad_step,
        {**base_plan[2], "step_index": 3, "parent_local_ref": "campaign", "expected_done_steps": 1},
    ]
    return plan, ad_step


def test_r4_creative_sources_not_matching_the_signed_depends_on_is_denied() -> None:
    """L1 (revision de seguridad 0.2.23): antes de esta regla, un binding
    podia declarar `creative_sources` DISTINTO del `depends_on` que el paso
    realmente firmo -- R4 nunca lo comprobaba (nombres de clave distintos a
    cada lado), y R7 lo habria resuelto igual usando ese valor colado."""
    signer, _ = _keypair()
    plan, ad_step = _upload_and_ad_step_plan()
    envelope = _envelope(step_plan=plan)
    approval = _signed_approval(signer, envelope)
    binding = _binding(
        ad_step,
        envelope_hash=_envelope_hash(envelope),
        parent_step_index=1,
        parent_entity_ref="act_100/as1",
        # El sobre firma `depends_on=["img#abc"]`; el binding declara OTRO.
        creative_sources=("img#other",),
    )
    after = {"child_plan": {"picture": "meta-image-hash-abc"}}
    ad_set_ref = EntityRef(
        PlatformCode.META, EntityLevel.AD_SET, "act_100/as1", _BUSINESS_ID, _CONNECTION_ID
    )
    diff_hash = compute_diff_hash(ad_set_ref, "new_ad:x", None, after)
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = WriteIntent(
        entity_ref=ad_set_ref,
        operation=WriteOperation.CREATE_AD,
        parametro="new_ad:x",
        valor_actual=None,
        valor_propuesto=after,
        diff_hash=diff_hash,
        expected_state_hash="",
        business_id=str(_BUSINESS_ID),
        package_binding=binding,
    )
    _, verifier = _keypair()
    upload_key = build_package_step_idempotency_key(_PUBLICATION_ID, 0)
    parent_key = build_package_step_idempotency_key(_PUBLICATION_ID, 1)

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=_fake_ledger(
            {upload_key: "meta-image-hash-abc", parent_key: "act_100/as1"}
        ),
    )

    assert denial is WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE


def test_r7_creative_source_resolved_end_to_end_through_the_real_ledger(tmp_path: Path) -> None:
    """H1 (revision de seguridad 0.2.23): companion positivo de
    `test_r7_creative_source_without_confirmed_upload_is_denied` -- una vez
    que la subida queda anotada en el libro REAL (`WriteLedgerStore`, nunca
    el resolver de juguete `_fake_ledger`) bajo su propia clave de
    idempotencia, `admit_package_step` para el `CREATE_AD` que depende de
    ella la resuelve sin preguntarle nada a `ads-api`."""
    signer, _ = _keypair()
    plan, ad_step = _upload_and_ad_step_plan()
    envelope = _envelope(step_plan=plan)
    approval = _signed_approval(signer, envelope)
    binding = _binding(
        ad_step,
        envelope_hash=_envelope_hash(envelope),
        parent_step_index=1,
        parent_entity_ref="act_100/as1",
        creative_sources=("img#abc",),
    )
    after = {"child_plan": {"picture": "meta-image-hash-abc"}}
    ad_set_ref = EntityRef(
        PlatformCode.META, EntityLevel.AD_SET, "act_100/as1", _BUSINESS_ID, _CONNECTION_ID
    )
    diff_hash = compute_diff_hash(ad_set_ref, "new_ad:x", None, after)
    authorization = _authorization(package_approval=approval, diff_hash=diff_hash)
    intent = WriteIntent(
        entity_ref=ad_set_ref,
        operation=WriteOperation.CREATE_AD,
        parametro="new_ad:x",
        valor_actual=None,
        valor_propuesto=after,
        diff_hash=diff_hash,
        expected_state_hash="",
        business_id=str(_BUSINESS_ID),
        package_binding=binding,
    )
    _, verifier = _keypair()
    ledger = WriteLedgerStore(tmp_path / "ledger.sqlite3")
    ledger.record_outcome_if_absent(
        build_package_step_idempotency_key(_PUBLICATION_ID, 0),
        WriteOutcome("SUCCEEDED", None, None, None, "meta-image-hash-abc"),
    )
    ledger.record_outcome_if_absent(
        build_package_step_idempotency_key(_PUBLICATION_ID, 1),
        WriteOutcome("SUCCEEDED", None, None, None, "act_100/as1"),
    )

    denial = admit_package_step(
        intent=intent,
        authorization=authorization,
        verifier=verifier,
        now=_NOW,
        resolve_created_resource=ledger.created_resource,
    )

    assert denial is None


# ---------------------------------------------------------------------------
# admit_package_upload -- H1 (revision de seguridad 0.2.23)
# ---------------------------------------------------------------------------

_UPLOAD_MEDIA = b"tiny-fake-png-bytes"
_UPLOAD_MIME_TYPE = "image/png"
_UPLOAD_WIDTH = 600
_UPLOAD_HEIGHT = 600


def _upload_template() -> dict[str, object]:
    return {
        "checksum": hashlib.sha256(_UPLOAD_MEDIA).hexdigest(),
        "mime_type": _UPLOAD_MIME_TYPE,
        "width": _UPLOAD_WIDTH,
        "height": _UPLOAD_HEIGHT,
    }


def _upload_only_step_plan() -> list[dict[str, object]]:
    template_hash = hashlib.sha256(canonical_json_bytes(_upload_template())).hexdigest()
    return [
        {
            "step_index": 0,
            "step_kind": "UPLOAD_CREATIVE",
            "local_ref": "img#abc",
            "parent_local_ref": None,
            "payload_template_hash": template_hash,
            "expected_done_steps": None,
            "depends_on": [],
        }
    ]


def _upload_fixture() -> tuple[ApprovalSigner, dict[str, object], dict[str, object]]:
    signer, _ = _keypair()
    plan = _upload_only_step_plan()
    envelope = _envelope(step_plan=plan)
    approval = _signed_approval(signer, envelope)
    binding = _binding(plan[0], envelope_hash=_envelope_hash(envelope))
    return signer, approval, binding


def test_admit_package_upload_happy_path_is_admitted() -> None:
    _, approval, binding = _upload_fixture()
    _, verifier = _keypair()

    denial = admit_package_upload(
        account_ref=AccountRef(PlatformCode.META, "act_100", _BUSINESS_ID, _CONNECTION_ID),
        media=_UPLOAD_MEDIA,
        mime_type=_UPLOAD_MIME_TYPE,
        width=_UPLOAD_WIDTH,
        height=_UPLOAD_HEIGHT,
        binding=binding,
        approval=approval,
        verifier=verifier,
        now=_NOW,
    )

    assert denial is None


def test_admit_package_upload_checksum_mismatch_is_denied() -> None:
    _, approval, binding = _upload_fixture()
    _, verifier = _keypair()

    denial = admit_package_upload(
        account_ref=AccountRef(PlatformCode.META, "act_100", _BUSINESS_ID, _CONNECTION_ID),
        media=b"these-are-not-the-approved-bytes",
        mime_type=_UPLOAD_MIME_TYPE,
        width=_UPLOAD_WIDTH,
        height=_UPLOAD_HEIGHT,
        binding=binding,
        approval=approval,
        verifier=verifier,
        now=_NOW,
    )

    assert denial is WriteDenialCode.CREATIVE_CHECKSUM_MISMATCH


def test_admit_package_upload_without_approval_is_denied() -> None:
    """R1, mitad "obligatorio": un binding sin sobre firmado nunca sube
    nada, ni siquiera si el resto de campos son perfectos."""
    _, _, binding = _upload_fixture()
    _, verifier = _keypair()

    denial = admit_package_upload(
        account_ref=AccountRef(PlatformCode.META, "act_100", _BUSINESS_ID, _CONNECTION_ID),
        media=_UPLOAD_MEDIA,
        mime_type=_UPLOAD_MIME_TYPE,
        width=_UPLOAD_WIDTH,
        height=_UPLOAD_HEIGHT,
        binding=binding,
        approval=None,
        verifier=verifier,
        now=_NOW,
    )

    assert denial is WriteDenialCode.PACKAGE_BINDING_REQUIRED


def test_admit_package_upload_wrong_step_kind_is_denied() -> None:
    """Un binding de OTRO tipo de paso (p.ej. `CREATE_AD`) nunca puede
    colarse por el camino de subida -- solo `UPLOAD_CREATIVE` firma una
    plantilla de `checksum`/`mime_type`/`width`/`height`."""
    signer, _ = _keypair()
    plan, ad_step = _upload_and_ad_step_plan()
    envelope = _envelope(step_plan=plan)
    approval = _signed_approval(signer, envelope)
    binding = _binding(
        ad_step,
        envelope_hash=_envelope_hash(envelope),
        parent_step_index=1,
        parent_entity_ref="act_100/as1",
        creative_sources=("img#abc",),
    )
    _, verifier = _keypair()

    denial = admit_package_upload(
        account_ref=AccountRef(PlatformCode.META, "act_100", _BUSINESS_ID, _CONNECTION_ID),
        media=_UPLOAD_MEDIA,
        mime_type=_UPLOAD_MIME_TYPE,
        width=_UPLOAD_WIDTH,
        height=_UPLOAD_HEIGHT,
        binding=binding,
        approval=approval,
        verifier=verifier,
        now=_NOW,
    )

    assert denial is WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE


def test_admit_package_upload_scope_mismatch_is_denied() -> None:
    """H-1: la cuenta que de verdad recibe la subida (resuelta por el
    bróker desde la conexion autenticada) tiene que ser byte-igual a la que
    el sobre firmo, no solo compatible."""
    _, approval, binding = _upload_fixture()
    _, verifier = _keypair()
    other_business, other_connection = uuid.uuid4(), uuid.uuid4()

    denial = admit_package_upload(
        account_ref=AccountRef(PlatformCode.META, "act_999", other_business, other_connection),
        media=_UPLOAD_MEDIA,
        mime_type=_UPLOAD_MIME_TYPE,
        width=_UPLOAD_WIDTH,
        height=_UPLOAD_HEIGHT,
        binding=binding,
        approval=approval,
        verifier=verifier,
        now=_NOW,
    )

    assert denial is WriteDenialCode.PACKAGE_SCOPE_MISMATCH
