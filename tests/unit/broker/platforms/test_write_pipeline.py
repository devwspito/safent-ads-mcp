"""`WriteAuthorizationPipeline`: orquesta la parte de los 8 controles de
contracts/platform-port.md que es comun a cualquier plataforma."""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent
from typing import Literal

import pytest

from safent_ads.accounts.application.ports import (
    AccountRef,
    PlatformAssetHandle,
    SignedAuthorization,
    WriteIntent,
    WriteOperation,
    WriteOutcome,
)
from safent_ads.accounts.domain.json_value import JsonValue
from safent_ads.broker.domain.package_admission import build_package_step_idempotency_key
from safent_ads.broker.domain.write_authorization import authorization_signing_payload
from safent_ads.broker.infrastructure.caps_config import parse_caps_config
from safent_ads.broker.infrastructure.write_ledger_store import WriteLedgerStore
from safent_ads.broker.platforms.write_pipeline import (
    PackageUploadDeniedError,
    WriteAuthorizationPipeline,
)
from safent_ads.proposals.domain.diff_hash import canonical_json_bytes, compute_diff_hash
from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from tests.unit.broker.ledger_scope_fakes import fake_scope

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_ACCOUNT = "1234567890"
_ENTITY_REF = EntityRef(
    PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, f"customers/{_ACCOUNT}/campaigns/1"
)
_STATE_HASH = "s" * 64

_CAPS_YAML = dedent(
    """
    defaults:
      max_step_pct: 100
      max_changes_per_day: 2
      autonomy_enabled: true
    accounts:
      "1234567890":
        daily_cap_minor: 100000
        monthly_cap_minor: 1000000
        floor_minor: 500
        ceiling_minor: 50000
    """
)


def _signer_and_verifier(seed_byte: bytes = b"9") -> tuple[ApprovalSigner, ApprovalVerifier]:
    seed_b64 = base64.b64encode(seed_byte * 32).decode()
    signer = ApprovalSigner.from_seed_b64(seed_b64)
    return signer, ApprovalVerifier.from_public_key_b64(signer.public_key_b64())


def _intent(
    *,
    before: JsonValue = "ACTIVE",
    after: JsonValue = "PAUSED",
    expected_state_hash: str = _STATE_HASH,
    parametro: str = "status",
    operation: WriteOperation = WriteOperation.PAUSE,
) -> WriteIntent:
    diff_hash = compute_diff_hash(_ENTITY_REF, parametro, before, after)
    return WriteIntent(
        business_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        entity_ref=_ENTITY_REF,
        operation=operation,
        parametro=parametro,
        valor_actual=before,
        valor_propuesto=after,
        diff_hash=diff_hash,
        expected_state_hash=expected_state_hash,
    )


def _authorization(
    signer: ApprovalSigner,
    *,
    diff_hash: str,
    kind: Literal["human_approval", "rule_authorization"] = "human_approval",
) -> SignedAuthorization:
    unsigned = SignedAuthorization(
        authorization_id="auth-1",
        proposal_id="proposal-1",
        kind=kind,
        diff_hash=diff_hash,
        guardrail_verdict_hash="v" * 64,
        issued_by="owner-1",
        expires_at=_NOW.replace(hour=13),
        signature="",
    )
    signature = signer.sign(authorization_signing_payload(unsigned)).hex()
    return SignedAuthorization(
        authorization_id=unsigned.authorization_id,
        proposal_id=unsigned.proposal_id,
        kind=unsigned.kind,
        diff_hash=unsigned.diff_hash,
        guardrail_verdict_hash=unsigned.guardrail_verdict_hash,
        issued_by=unsigned.issued_by,
        expires_at=unsigned.expires_at,
        signature=signature,
    )


def _pipeline(tmp_path: Path, *, verifier: ApprovalVerifier) -> WriteAuthorizationPipeline:
    caps = parse_caps_config(_CAPS_YAML)
    ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
    return WriteAuthorizationPipeline(verifier, caps, ledger, scope_resolver=fake_scope)


def test_authorize_returns_none_when_every_control_passes(tmp_path: Path) -> None:
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    intent = _intent()
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = pipeline.authorize(
        intent,
        authorization,
        platform_account_id=_ACCOUNT,
        remote_state_hash=_STATE_HASH,
        now=_NOW,
    )

    assert outcome is None


def test_valid_rule_signature_does_not_replace_owner_approval(tmp_path: Path) -> None:
    signer, verifier = _signer_and_verifier()
    intent = _intent()
    outcome = _pipeline(tmp_path, verifier=verifier).authorize(
        intent,
        _authorization(signer, diff_hash=intent.diff_hash, kind="rule_authorization"),
        platform_account_id=_ACCOUNT,
        remote_state_hash=_STATE_HASH,
        now=_NOW,
    )
    assert outcome is not None
    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "owner_approval_required"


def test_authorize_denies_when_the_signature_does_not_verify(tmp_path: Path) -> None:
    signer, _own_verifier = _signer_and_verifier(b"9")
    _, other_verifier = _signer_and_verifier(b"8")
    pipeline = _pipeline(tmp_path, verifier=other_verifier)
    intent = _intent()
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = pipeline.authorize(
        intent,
        authorization,
        platform_account_id=_ACCOUNT,
        remote_state_hash=_STATE_HASH,
        now=_NOW,
    )

    assert outcome is not None
    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "invalid_signature"


def test_authorize_blocks_on_the_hard_cap_before_looking_at_drift(tmp_path: Path) -> None:
    """Si el tope duro Y la deriva fallarian a la vez, el veredicto es
    `BLOCKED_HARD_CAP`: el tope se evalua antes que la precondicion de
    estado remoto."""
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    intent = _intent(
        before={"amount": "100.00", "currency": "EUR"},
        after={"amount": "40000.00", "currency": "EUR"},
        parametro="daily_budget",
        operation=WriteOperation.RAISE_BUDGET,
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = pipeline.authorize(
        intent,
        authorization,
        platform_account_id=_ACCOUNT,
        remote_state_hash="drift" * 16,
        now=_NOW,
    )

    assert outcome is not None
    assert outcome.outcome == "BLOCKED_HARD_CAP"


def test_authorize_skips_drift_when_the_remote_state_moved(tmp_path: Path) -> None:
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    intent = _intent()
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = pipeline.authorize(
        intent,
        authorization,
        platform_account_id=_ACCOUNT,
        remote_state_hash="d" * 64,
        now=_NOW,
    )

    assert outcome is not None
    assert outcome.outcome == "SKIPPED_DRIFT"


def test_authorize_denies_for_an_account_without_caps_configured(tmp_path: Path) -> None:
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    intent = _intent()
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = pipeline.authorize(
        intent,
        authorization,
        platform_account_id="no-tal-cuenta",
        remote_state_hash=_STATE_HASH,
        now=_NOW,
    )

    assert outcome is not None
    assert outcome.outcome == "DENIED"
    assert outcome.error_code == "account_not_configured"


def test_creacion_pmax_pasa_por_el_tope_duro_con_el_importe_correcto(tmp_path: Path) -> None:
    """T-7 (BL-5): T014 hizo de `_google`/`creation_budget` la autoridad
    unica del nativo de PMax -- el tope duro del broker debe leer el
    importe correcto por ese mismo camino, no denegar por
    `AMOUNT_UNREADABLE`."""
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    payload = {
        "creation_plan": {
            "schema_version": 1,
            "platform": "google",
            "name": "Performance Max proposal",
            "status": "PAUSED",
            "daily_budget": {"amount": "20.00", "currency": "EUR"},
            "native": {
                "advertising_channel_type": "PERFORMANCE_MAX",
                "bidding_strategy": {"kind": "MAXIMIZE_CONVERSION_VALUE", "target_roas": "4.00"},
                "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
                "conversion_goals": [
                    {"resource_name": f"customers/{_ACCOUNT}/conversionActions/1"}
                ],
                "url_expansion_opt_out": True,
                "text_asset_automation_enabled": False,
            },
        }
    }
    ref = EntityRef.parse(f"google:account:{uuid.uuid4()}:{uuid.uuid4()}:{_ACCOUNT}")
    intent = WriteIntent(
        ref,
        WriteOperation.CREATE_CAMPAIGN,
        "new_campaign:pmax",
        None,
        payload,
        compute_diff_hash(ref, "new_campaign:pmax", None, payload),
        "",
        str(ref.business_id),
    )
    authorization = _authorization(signer, diff_hash=intent.diff_hash)

    outcome = pipeline.authorize(
        intent,
        authorization,
        platform_account_id=_ACCOUNT,
        remote_state_hash="",
        now=_NOW,
    )

    assert outcome is None


def test_replay_returns_original_outcome_without_a_second_mutate(tmp_path: Path) -> None:
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    intent = _intent()

    auth = _authorization(signer, diff_hash=intent.diff_hash)
    assert pipeline.begin_write("key-1", intent, auth, _ACCOUNT, _NOW) is None
    first = pipeline.finalize(
        "key-1",
        _ACCOUNT,
        intent,
        _succeeded_outcome(state_hash_after="a" * 64),
        now=_NOW,
    )
    replay = pipeline.begin_write("key-1", intent, auth, _ACCOUNT, _NOW)

    assert replay == first
    assert replay is not None
    assert replay.state_hash_after == "a" * 64


def test_finalize_records_the_applied_delta_only_on_success(tmp_path: Path) -> None:
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    intent = _intent(
        before={"amount": "100.00", "currency": "EUR"},
        after={"amount": "70.00", "currency": "EUR"},
        parametro="daily_budget",
        operation=WriteOperation.LOWER_BUDGET,
    )

    assert (
        pipeline.begin_write(
            "key-1", intent, _authorization(signer, diff_hash=intent.diff_hash), _ACCOUNT, _NOW
        )
        is None
    )
    pipeline.finalize(
        "key-1", _ACCOUNT, intent, _succeeded_outcome(state_hash_after="a" * 64), now=_NOW
    )

    ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
    snapshot = ledger.snapshot_today(fake_scope(intent, _ACCOUNT), _NOW.date())
    assert snapshot.changes_count == 1
    assert snapshot.applied_delta_minor_units == -3_000


def test_finalize_does_not_record_a_delta_for_a_failed_outcome(tmp_path: Path) -> None:
    signer, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    intent = _intent(
        before={"amount": "100.00", "currency": "EUR"},
        after={"amount": "70.00", "currency": "EUR"},
        parametro="daily_budget",
        operation=WriteOperation.LOWER_BUDGET,
    )

    assert (
        pipeline.begin_write(
            "key-1", intent, _authorization(signer, diff_hash=intent.diff_hash), _ACCOUNT, _NOW
        )
        is None
    )
    pipeline.finalize(
        "key-1",
        _ACCOUNT,
        intent,
        _succeeded_outcome(outcome="FAILED", state_hash_after=None, error_code="sdk_error"),
        now=_NOW,
    )

    ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
    snapshot = ledger.snapshot_today(fake_scope(intent, _ACCOUNT), _NOW.date())
    assert snapshot.changes_count == 0


def _succeeded_outcome(
    *,
    outcome: str = "SUCCEEDED",
    state_hash_after: str | None,
    error_code: str | None = None,
) -> WriteOutcome:
    return WriteOutcome(
        outcome=outcome,  # type: ignore[arg-type]
        applied_value={"amount": "70.00", "currency": "EUR"},
        state_hash_after=state_hash_after,
        error_code=error_code,
        platform_request_id="req-1",
    )


# ---------------------------------------------------------------------------
# admit_upload -- H1 (revision de seguridad 0.2.23): un paso UPLOAD_CREATIVE
# pasa por las mismas R1-R6 de `admit_package_step` mas el cotejo de
# `sha256(media)`, y el manejador confirmado queda anotado en el MISMO
# `WriteLedgerStore` que resuelve `{creative_of:X}` para el `CREATE_AD` que
# depende de el.
# ---------------------------------------------------------------------------

_UPLOAD_BUSINESS_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
_UPLOAD_CONNECTION_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
_UPLOAD_ACCOUNT_REF = AccountRef(
    PlatformCode.META, "act_500", _UPLOAD_BUSINESS_ID, _UPLOAD_CONNECTION_ID
)
_UPLOAD_PUBLICATION_ID = "pub-upload-1"
_UPLOAD_MEDIA = b"tiny-fake-png-bytes"
_UPLOAD_MIME_TYPE = "image/png"
_UPLOAD_WIDTH = 600
_UPLOAD_HEIGHT = 600


def _upload_template(media: bytes = _UPLOAD_MEDIA) -> dict[str, object]:
    return {
        "checksum": hashlib.sha256(media).hexdigest(),
        "mime_type": _UPLOAD_MIME_TYPE,
        "width": _UPLOAD_WIDTH,
        "height": _UPLOAD_HEIGHT,
    }


def _upload_step_plan() -> list[dict[str, object]]:
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


def _upload_envelope() -> dict[str, object]:
    return {
        "envelope_version": 2,
        "package_id": "pkg-upload-1",
        "package_hash": "h" * 64,
        "business_id": str(_UPLOAD_BUSINESS_ID),
        "platform": "meta",
        "account_ref": str(_UPLOAD_ACCOUNT_REF),
        "publication_id": _UPLOAD_PUBLICATION_ID,
        "approved_by": "owner-1",
        "approved_at": _NOW.isoformat(),
        "approval_expires_at": _NOW.replace(hour=13).isoformat(),
        "step_plan": _upload_step_plan(),
    }


def _upload_signed_approval(
    signer: ApprovalSigner, envelope: dict[str, object]
) -> dict[str, object]:
    signature = signer.sign(envelope)
    return {
        "envelope": envelope,
        "authorization_id": "human-auth-1",
        "issued_by": envelope["approved_by"],
        "expires_at": envelope["approval_expires_at"],
        "signature": signature.hex(),
    }


def _upload_binding(envelope: dict[str, object]) -> dict[str, object]:
    step = envelope["step_plan"][0]  # type: ignore[index]
    return {
        "package_id": envelope["package_id"],
        "package_hash": envelope["package_hash"],
        "publication_id": envelope["publication_id"],
        "envelope_hash": hashlib.sha256(canonical_json_bytes(envelope)).hexdigest(),
        "step_index": step["step_index"],
        "step_kind": step["step_kind"],
        "local_ref": step["local_ref"],
        "parent_local_ref": step["parent_local_ref"],
        "parent_step_index": None,
        "parent_entity_ref": None,
        "account_ref": envelope["account_ref"],
        "payload_template_hash": step["payload_template_hash"],
        "creative_sources": [],
        "expected_done_steps": step["expected_done_steps"],
    }


async def test_admit_upload_calls_the_platform_directly_for_a_standalone_request(
    tmp_path: Path,
) -> None:
    """Item 5 (H1): `upload_creative_asset` independiente -- sin `binding`
    ni `approval` -- nunca toca `admit_package_upload` ni el libro."""
    _, verifier = _signer_and_verifier()
    pipeline = _pipeline(tmp_path, verifier=verifier)
    handle = PlatformAssetHandle(platform_asset_id="standalone-hash", preview_url=None)
    calls = 0

    async def _upload() -> PlatformAssetHandle:
        nonlocal calls
        calls += 1
        return handle

    result = await pipeline.admit_upload(
        account_ref=_UPLOAD_ACCOUNT_REF,
        media=_UPLOAD_MEDIA,
        mime_type=_UPLOAD_MIME_TYPE,
        width=_UPLOAD_WIDTH,
        height=_UPLOAD_HEIGHT,
        binding=None,
        approval=None,
        upload=_upload,
        now=_NOW,
    )

    assert result == handle
    assert calls == 1


async def test_admit_upload_denies_a_checksum_mismatch_without_calling_the_platform(
    tmp_path: Path,
) -> None:
    signer, verifier = _signer_and_verifier()
    envelope = _upload_envelope()
    approval = _upload_signed_approval(signer, envelope)
    binding = _upload_binding(envelope)
    pipeline = _pipeline(tmp_path, verifier=verifier)
    calls = 0

    async def _upload() -> PlatformAssetHandle:
        nonlocal calls
        calls += 1
        return PlatformAssetHandle(platform_asset_id="unused", preview_url=None)

    with pytest.raises(PackageUploadDeniedError) as excinfo:
        await pipeline.admit_upload(
            account_ref=_UPLOAD_ACCOUNT_REF,
            media=b"different-bytes-than-what-was-signed",
            mime_type=_UPLOAD_MIME_TYPE,
            width=_UPLOAD_WIDTH,
            height=_UPLOAD_HEIGHT,
            binding=binding,
            approval=approval,
            upload=_upload,
            now=_NOW,
        )

    assert excinfo.value.error_code == "creative_checksum_mismatch"
    assert calls == 0


async def test_admit_upload_records_the_handle_and_replays_it_without_a_second_upload(
    tmp_path: Path,
) -> None:
    signer, verifier = _signer_and_verifier()
    envelope = _upload_envelope()
    approval = _upload_signed_approval(signer, envelope)
    binding = _upload_binding(envelope)
    pipeline = _pipeline(tmp_path, verifier=verifier)
    handle = PlatformAssetHandle(
        platform_asset_id="meta-image-hash-1", preview_url="https://graph.facebook.com/x"
    )
    calls = 0

    async def _upload() -> PlatformAssetHandle:
        nonlocal calls
        calls += 1
        return handle

    kwargs: dict[str, object] = {
        "account_ref": _UPLOAD_ACCOUNT_REF,
        "media": _UPLOAD_MEDIA,
        "mime_type": _UPLOAD_MIME_TYPE,
        "width": _UPLOAD_WIDTH,
        "height": _UPLOAD_HEIGHT,
        "binding": binding,
        "approval": approval,
        "upload": _upload,
        "now": _NOW,
    }

    first = await pipeline.admit_upload(**kwargs)  # type: ignore[arg-type]
    second = await pipeline.admit_upload(**kwargs)  # type: ignore[arg-type]

    assert first.platform_asset_id == "meta-image-hash-1"
    assert second.platform_asset_id == "meta-image-hash-1"
    assert calls == 1

    ledger = WriteLedgerStore(tmp_path / "write_ledger.sqlite3")
    key = build_package_step_idempotency_key(_UPLOAD_PUBLICATION_ID, 0)
    assert ledger.created_resource(key) == "meta-image-hash-1"
