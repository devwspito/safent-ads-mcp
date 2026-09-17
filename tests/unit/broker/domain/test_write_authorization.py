"""`broker/domain/write_authorization.py`: los controles puros que el
broker aplica antes de mutar (contracts/platform-port.md comprobaciones
3-7)."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime

from safent_ads.accounts.application.ports import SignedAuthorization, WriteIntent, WriteOperation
from safent_ads.accounts.domain.json_value import JsonValue
from safent_ads.broker.domain.write_authorization import (
    WriteDenialCode,
    authorization_signing_payload,
    check_hard_caps,
    check_state_drift,
    money_minor_units,
    outcome_literal_for_denial,
    platform_account_id_from_google_resource_name,
    recompute_diff_hash,
    verify_authorization,
)
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.shared.crypto.ed25519 import ApprovalSigner, ApprovalVerifier
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_ENTITY_REF = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "customers/1/campaigns/2")


class _AccountCaps:
    def __init__(
        self,
        *,
        daily_cap_minor: int = 100_000,
        monthly_cap_minor: int = 1_000_000,
        floor_minor: int = 500,
        ceiling_minor: int = 50_000,
        max_step_pct: float = 30.0,
        max_changes_per_day: int = 2,
    ) -> None:
        self.daily_cap_minor = daily_cap_minor
        self.monthly_cap_minor = monthly_cap_minor
        self.floor_minor = floor_minor
        self.ceiling_minor = ceiling_minor
        self.max_step_pct = max_step_pct
        self.max_changes_per_day = max_changes_per_day


def _keypair(seed_byte: bytes = b"5") -> tuple[ApprovalSigner, ApprovalVerifier]:
    seed_b64 = base64.b64encode(seed_byte * 32).decode()
    signer = ApprovalSigner.from_seed_b64(seed_b64)
    return signer, ApprovalVerifier.from_public_key_b64(signer.public_key_b64())


def _intent(
    *,
    parametro: str = "status",
    before: JsonValue = "ACTIVE",
    after: JsonValue = "PAUSED",
    operation: WriteOperation = WriteOperation.PAUSE,
    expected_state_hash: str = "h" * 64,
) -> WriteIntent:
    diff_hash = compute_diff_hash(_ENTITY_REF, parametro, before, after)
    return WriteIntent(
        entity_ref=_ENTITY_REF,
        operation=operation,
        parametro=parametro,
        valor_actual=before,
        valor_propuesto=after,
        diff_hash=diff_hash,
        expected_state_hash=expected_state_hash,
    )


_DEFAULT_EXPIRY = _NOW.replace(hour=13)


def _signed_authorization(
    signer: ApprovalSigner,
    *,
    diff_hash: str,
    kind: str = "human_approval",
    expires_at: datetime = _DEFAULT_EXPIRY,
) -> SignedAuthorization:
    unsigned = SignedAuthorization(
        authorization_id="auth-1",
        proposal_id="proposal-1",
        kind=kind,  # type: ignore[arg-type]
        diff_hash=diff_hash,
        guardrail_verdict_hash="v" * 64,
        issued_by="owner-1",
        expires_at=expires_at,
        signature="",
    )
    payload = authorization_signing_payload(unsigned)
    signature = signer.sign(payload).hex()
    return _replace_signature(unsigned, signature)


def _replace_signature(authorization: SignedAuthorization, signature: str) -> SignedAuthorization:
    return SignedAuthorization(
        authorization_id=authorization.authorization_id,
        proposal_id=authorization.proposal_id,
        kind=authorization.kind,
        diff_hash=authorization.diff_hash,
        guardrail_verdict_hash=authorization.guardrail_verdict_hash,
        issued_by=authorization.issued_by,
        expires_at=authorization.expires_at,
        signature=signature,
    )


def test_recompute_diff_hash_matches_the_shared_projection() -> None:
    intent = _intent()

    assert recompute_diff_hash(intent) == compute_diff_hash(
        intent.entity_ref, intent.parametro, intent.valor_actual, intent.valor_propuesto
    )


def test_verify_authorization_accepts_a_valid_human_approval() -> None:
    signer, verifier = _keypair()
    intent = _intent()
    authorization = _signed_authorization(signer, diff_hash=intent.diff_hash)

    denial = verify_authorization(intent, authorization, intent.diff_hash, _NOW, verifier)

    assert denial is None


def test_diff_swap_between_intent_and_authorization_is_denied() -> None:
    """T-60: `intent.diff_hash` y `authorization.diff_hash` deben citar el
    mismo cambio recomputado -- una autorizacion valida para OTRO payload
    no vale para este."""
    signer, verifier = _keypair()
    intent = _intent()
    other_intent = _intent(before="ACTIVE", after="REMOVED")
    authorization = _signed_authorization(signer, diff_hash=other_intent.diff_hash)

    denial = verify_authorization(intent, authorization, intent.diff_hash, _NOW, verifier)

    assert denial is WriteDenialCode.DIFF_HASH_MISMATCH


def test_expired_authorization_denied() -> None:
    signer, verifier = _keypair()
    intent = _intent()
    authorization = _signed_authorization(
        signer, diff_hash=intent.diff_hash, expires_at=_NOW.replace(hour=11)
    )

    denial = verify_authorization(intent, authorization, intent.diff_hash, _NOW, verifier)

    assert denial is WriteDenialCode.EXPIRED


def test_authorization_expiring_exactly_now_is_denied() -> None:
    signer, verifier = _keypair()
    intent = _intent()
    authorization = _signed_authorization(signer, diff_hash=intent.diff_hash, expires_at=_NOW)

    denial = verify_authorization(intent, authorization, intent.diff_hash, _NOW, verifier)

    assert denial is WriteDenialCode.EXPIRED


def test_rule_authorization_cannot_raise_budget() -> None:
    signer, verifier = _keypair()
    intent = _intent(
        parametro="daily_budget",
        before={"amount": "70.00", "currency": "EUR"},
        after={"amount": "100.00", "currency": "EUR"},
        operation=WriteOperation.RAISE_BUDGET,
    )
    authorization = _signed_authorization(
        signer, diff_hash=intent.diff_hash, kind="rule_authorization"
    )

    denial = verify_authorization(intent, authorization, intent.diff_hash, _NOW, verifier)

    assert denial is WriteDenialCode.RULE_AUTHORIZATION_CANNOT_INCREASE_SPEND


def test_rule_authorization_can_still_lower_budget() -> None:
    signer, verifier = _keypair()
    intent = _intent(
        parametro="daily_budget",
        before={"amount": "100.00", "currency": "EUR"},
        after={"amount": "70.00", "currency": "EUR"},
        operation=WriteOperation.LOWER_BUDGET,
    )
    authorization = _signed_authorization(
        signer, diff_hash=intent.diff_hash, kind="rule_authorization"
    )

    denial = verify_authorization(intent, authorization, intent.diff_hash, _NOW, verifier)

    assert denial is None


def test_rule_authorization_mislabelled_as_lower_budget_but_actually_raising_is_denied() -> None:
    """Security review F4, item 7: el sentido de la escritura se deriva de
    `valor_actual`/`valor_propuesto`, no del campo `operation` declarado
    por el cliente -- un `operation` mal etiquetado no debe poder saltarse
    la regla."""
    signer, verifier = _keypair()
    intent = _intent(
        parametro="daily_budget",
        before={"amount": "70.00", "currency": "EUR"},
        after={"amount": "100.00", "currency": "EUR"},
        operation=WriteOperation.LOWER_BUDGET,
    )
    authorization = _signed_authorization(
        signer, diff_hash=intent.diff_hash, kind="rule_authorization"
    )

    denial = verify_authorization(intent, authorization, intent.diff_hash, _NOW, verifier)

    assert denial is WriteDenialCode.RULE_AUTHORIZATION_CANNOT_INCREASE_SPEND


def test_signature_mismatch_denied() -> None:
    signer, _ = _keypair(b"5")
    _, other_verifier = _keypair(b"6")
    intent = _intent()
    authorization = _signed_authorization(signer, diff_hash=intent.diff_hash)

    denial = verify_authorization(intent, authorization, intent.diff_hash, _NOW, other_verifier)

    assert denial is WriteDenialCode.INVALID_SIGNATURE


def test_tampered_signature_bytes_are_denied() -> None:
    signer, verifier = _keypair()
    intent = _intent()
    authorization = _signed_authorization(signer, diff_hash=intent.diff_hash)
    tampered = _replace_signature(authorization, "00" * 64)

    denial = verify_authorization(intent, tampered, intent.diff_hash, _NOW, verifier)

    assert denial is WriteDenialCode.INVALID_SIGNATURE


def test_money_minor_units_parses_a_money_shaped_value() -> None:
    assert money_minor_units({"amount": "70.50", "currency": "EUR"}) == 7050


def test_money_minor_units_returns_none_for_non_money_values() -> None:
    assert money_minor_units("PAUSED") is None
    assert money_minor_units(None) is None
    assert money_minor_units({"clauses": []}) is None


def test_money_minor_units_returns_none_for_a_creation_plan_creation_budget_rejects() -> None:
    """`creation_budget`/`_google` (T014, BL-5) es el unico lector del
    nativo de creacion; si lo rechaza, `money_minor_units` nunca propaga
    la excepcion -- solo `None`, para que `check_hard_caps` decida (T-7)."""
    assert money_minor_units({"creation_plan": {"schema_version": 1}}) is None


def test_broker_hard_cap_blocks_max_changes_per_day() -> None:
    denial = check_hard_caps(
        _AccountCaps(max_changes_per_day=2),
        before_minor_units=10_000,
        after_minor_units=9_000,
        changes_count_today=2,
        applied_delta_today_minor_units=0,
        applied_delta_month_to_date_minor_units=0,
    )

    assert denial is WriteDenialCode.MAX_CHANGES_PER_DAY


def test_broker_hard_cap_blocks_below_floor() -> None:
    denial = check_hard_caps(
        _AccountCaps(floor_minor=500),
        before_minor_units=600,
        after_minor_units=400,
        changes_count_today=0,
        applied_delta_today_minor_units=0,
        applied_delta_month_to_date_minor_units=0,
    )

    assert denial is WriteDenialCode.FLOOR_EXCEEDED


def test_broker_hard_cap_blocks_above_ceiling() -> None:
    denial = check_hard_caps(
        _AccountCaps(ceiling_minor=50_000, max_step_pct=100.0),
        before_minor_units=40_000,
        after_minor_units=60_000,
        changes_count_today=0,
        applied_delta_today_minor_units=0,
        applied_delta_month_to_date_minor_units=0,
    )

    assert denial is WriteDenialCode.CEILING_EXCEEDED


def test_broker_hard_cap_blocks_a_step_beyond_max_step_pct() -> None:
    denial = check_hard_caps(
        _AccountCaps(max_step_pct=30.0, ceiling_minor=1_000_000),
        before_minor_units=10_000,
        after_minor_units=20_000,
        changes_count_today=0,
        applied_delta_today_minor_units=0,
        applied_delta_month_to_date_minor_units=0,
    )

    assert denial is WriteDenialCode.MAX_STEP_EXCEEDED


def test_broker_hard_cap_blocks_the_daily_cap() -> None:
    denial = check_hard_caps(
        _AccountCaps(daily_cap_minor=10_000, max_step_pct=100.0, ceiling_minor=1_000_000),
        before_minor_units=5_000,
        after_minor_units=8_000,
        changes_count_today=0,
        applied_delta_today_minor_units=9_000,
        applied_delta_month_to_date_minor_units=0,
    )

    assert denial is WriteDenialCode.DAILY_CAP_EXCEEDED


def test_broker_hard_cap_blocks_the_monthly_cap() -> None:
    denial = check_hard_caps(
        _AccountCaps(
            daily_cap_minor=1_000_000,
            monthly_cap_minor=10_000,
            max_step_pct=100.0,
            ceiling_minor=1_000_000,
        ),
        before_minor_units=5_000,
        after_minor_units=8_000,
        changes_count_today=0,
        applied_delta_today_minor_units=0,
        applied_delta_month_to_date_minor_units=9_000,
    )

    assert denial is WriteDenialCode.MONTHLY_CAP_EXCEEDED


def test_broker_hard_cap_allows_a_change_within_every_limit() -> None:
    denial = check_hard_caps(
        _AccountCaps(),
        before_minor_units=10_000,
        after_minor_units=9_000,
        changes_count_today=0,
        applied_delta_today_minor_units=0,
        applied_delta_month_to_date_minor_units=0,
    )

    assert denial is None


def test_broker_hard_cap_ignores_money_bounds_for_non_money_operations() -> None:
    """`max_changes_per_day` se aplica siempre; suelo/techo/paso/tope solo
    tienen sentido cuando hay un importe que comparar."""
    denial = check_hard_caps(
        _AccountCaps(max_changes_per_day=5),
        before_minor_units=None,
        after_minor_units=None,
        changes_count_today=0,
        applied_delta_today_minor_units=0,
        applied_delta_month_to_date_minor_units=0,
    )

    assert denial is None


def test_importe_ilegible_con_is_creation_deniega_no_permite() -> None:
    """T-7 (BL-5): una creacion con importe ilegible nunca cae en el
    `return None` generico -- deniega, no permite."""
    denial = check_hard_caps(
        _AccountCaps(),
        before_minor_units=0,
        after_minor_units=None,
        changes_count_today=0,
        applied_delta_today_minor_units=0,
        applied_delta_month_to_date_minor_units=0,
        is_creation=True,
    )

    assert denial is WriteDenialCode.AMOUNT_UNREADABLE


def test_importe_ilegible_sin_is_creation_sigue_sin_aplicar_topes_de_dinero() -> None:
    """El fail-closed nuevo es exclusivo de `is_creation=True`: una
    operacion que no es dinero (p.ej. un cambio de estado) sigue sin
    tener topes de importe que aplicar."""
    denial = check_hard_caps(
        _AccountCaps(),
        before_minor_units=None,
        after_minor_units=None,
        changes_count_today=0,
        applied_delta_today_minor_units=0,
        applied_delta_month_to_date_minor_units=0,
        is_creation=False,
    )

    assert denial is None


def test_drift_skips_write_on_hash_mismatch() -> None:
    denial = check_state_drift("a" * 64, "b" * 64)

    assert denial is WriteDenialCode.STATE_DRIFT


def test_drift_check_passes_when_hashes_match() -> None:
    assert check_state_drift("a" * 64, "a" * 64) is None


def test_an_empty_expected_hash_is_treated_as_drift() -> None:
    """`expected_state_hash` vacio (propuesta sin estado previo) nunca
    iguala un digest real -- ninguna operacion mutable de este broker
    actua sobre una entidad inexistente."""
    assert check_state_drift("a" * 64, "") is WriteDenialCode.STATE_DRIFT


def test_outcome_literal_maps_every_denial_to_a_write_outcome_literal() -> None:
    for code in WriteDenialCode:
        assert outcome_literal_for_denial(code) in {"DENIED", "BLOCKED_HARD_CAP", "SKIPPED_DRIFT"}


def test_platform_account_id_from_google_resource_name_extracts_customer_id() -> None:
    assert (
        platform_account_id_from_google_resource_name("customers/1234567890/campaigns/1")
        == "1234567890"
    )


def test_platform_account_id_from_google_resource_name_rejects_malformed_input() -> None:
    assert platform_account_id_from_google_resource_name("not-a-resource-name") is None


def test_authorization_signing_payload_carries_exactly_the_wire_fields() -> None:
    authorization = SignedAuthorization(
        authorization_id="auth-1",
        proposal_id="proposal-1",
        kind="human_approval",
        diff_hash="d" * 64,
        guardrail_verdict_hash="v" * 64,
        issued_by="owner-1",
        expires_at=_NOW,
        signature="",
    )

    payload = authorization_signing_payload(authorization)

    assert isinstance(payload, Mapping)
    assert set(payload) == {
        "authorization_id",
        "proposal_id",
        "kind",
        "diff_hash",
        "guardrail_verdict_hash",
        "issued_by",
        "expires_at",
    }
