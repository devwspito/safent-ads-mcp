"""Decisiones puras de los controles 3-6 y 8 de `contracts/platform-port.md`
("Comprobaciones del broker antes de cualquier escritura"): recomputar
`diff_hash`, verificar la autorizacion Ed25519 (caducidad + regla de
`kind`), y aplicar los topes duros propios del broker. Sin I/O: el llamante
(`broker/platforms/write_pipeline.py`) resuelve cuenta, caps y ledger antes
de invocar estas funciones -- igual que `execution/domain/guardrails.py`
mantiene `GuardrailEvaluator.evaluate` puro y recibe el `LedgerSnapshot` ya
leido."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal, Protocol, cast

from safent_ads.accounts.application.ports import SignedAuthorization, WriteIntent
from safent_ads.accounts.domain.json_value import JsonValue
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError, creation_budget
from safent_ads.proposals.domain.diff_hash import compute_diff_hash

_RULE_AUTHORIZATION: Literal["rule_authorization"] = "rule_authorization"


class WriteDenialCode(StrEnum):
    """Motivo por el que el broker no procede con la escritura. El llamante
    lo traduce a `WriteOutcome.outcome` (`DENIED`/`BLOCKED_HARD_CAP`/
    `SKIPPED_DRIFT`) -- nunca una excepcion (contracts/platform-port.md:
    "nunca fail-open", pero tampoco fail-loud hacia el llamante: un
    veredicto denegado sigue siendo una respuesta valida)."""

    DIFF_HASH_MISMATCH = "diff_hash_mismatch"
    EXPIRED = "authorization_expired"
    INVALID_SIGNATURE = "invalid_signature"
    RULE_AUTHORIZATION_CANNOT_INCREASE_SPEND = "rule_authorization_cannot_increase_spend"
    ACCOUNT_NOT_CONFIGURED = "account_not_configured"
    LEDGER_SCOPE_UNVERIFIED = "ledger_scope_unverified"
    LEGACY_LEDGER_SCOPE_UNRESOLVED = "legacy_ledger_scope_unresolved"
    MAX_CHANGES_PER_DAY = "max_changes_per_day_reached"
    AMOUNT_UNREADABLE = "amount_unreadable"
    FLOOR_EXCEEDED = "floor_exceeded"
    CEILING_EXCEEDED = "ceiling_exceeded"
    MAX_STEP_EXCEEDED = "max_step_exceeded"
    DAILY_CAP_EXCEEDED = "daily_cap_exceeded"
    MONTHLY_CAP_EXCEEDED = "monthly_cap_exceeded"
    STATE_DRIFT = "state_drift"
    OPERATION_NOT_SUPPORTED = "operation_not_supported"
    WRITE_PATH_NOT_WIRED = "write_path_not_wired"
    OPERATION_DIFF_MISMATCH = "operation_diff_mismatch"
    RULE_OPERATION_NOT_DEFENSIVE = "rule_operation_not_defensive"
    OWNER_APPROVAL_REQUIRED = "owner_approval_required"
    MANAGED_BINDING_MISMATCH = "managed_binding_mismatch"
    MANAGED_ADMISSION_UNAVAILABLE = "managed_admission_unavailable"
    MANAGED_ADMISSION_DENIED = "managed_admission_denied"
    # `003-paquete-de-campana` contracts/api.md §R2.E (BL-2/BL-3): las diez
    # denegaciones de la admision de un `package_step` (R1-R7 mas
    # `creative_checksum_mismatch`, reservado para T111). Todas `DENIED`
    # -- ninguna es un tope duro ni una deriva de estado.
    PACKAGE_BINDING_REQUIRED = "package_binding_required"
    PACKAGE_BINDING_NOT_ALLOWED = "package_binding_not_allowed"
    PACKAGE_APPROVAL_INVALID = "package_approval_invalid"
    PACKAGE_APPROVAL_EXPIRED = "package_approval_expired"
    PACKAGE_CHANGED = "package_changed"
    PACKAGE_BINDING_NOT_DERIVABLE = "package_binding_not_derivable"
    PACKAGE_PARENT_UNCONFIRMED = "package_parent_unconfirmed"
    PACKAGE_SCOPE_MISMATCH = "package_scope_mismatch"
    PACKAGE_PAYLOAD_NOT_REPRODUCIBLE = "package_payload_not_reproducible"
    CREATIVE_CHECKSUM_MISMATCH = "creative_checksum_mismatch"


_WriteVerdict = Literal["DENIED", "BLOCKED_HARD_CAP", "SKIPPED_DRIFT"]

_OUTCOME_BY_DENIAL: Mapping[WriteDenialCode, _WriteVerdict] = {
    WriteDenialCode.MANAGED_BINDING_MISMATCH: "DENIED",
    WriteDenialCode.MANAGED_ADMISSION_UNAVAILABLE: "DENIED",
    WriteDenialCode.MANAGED_ADMISSION_DENIED: "DENIED",
    WriteDenialCode.DIFF_HASH_MISMATCH: "DENIED",
    WriteDenialCode.EXPIRED: "DENIED",
    WriteDenialCode.INVALID_SIGNATURE: "DENIED",
    WriteDenialCode.RULE_AUTHORIZATION_CANNOT_INCREASE_SPEND: "DENIED",
    WriteDenialCode.ACCOUNT_NOT_CONFIGURED: "DENIED",
    WriteDenialCode.LEDGER_SCOPE_UNVERIFIED: "DENIED",
    WriteDenialCode.LEGACY_LEDGER_SCOPE_UNRESOLVED: "DENIED",
    WriteDenialCode.OPERATION_NOT_SUPPORTED: "DENIED",
    WriteDenialCode.WRITE_PATH_NOT_WIRED: "DENIED",
    WriteDenialCode.OPERATION_DIFF_MISMATCH: "DENIED",
    WriteDenialCode.RULE_OPERATION_NOT_DEFENSIVE: "DENIED",
    WriteDenialCode.OWNER_APPROVAL_REQUIRED: "DENIED",
    WriteDenialCode.MAX_CHANGES_PER_DAY: "BLOCKED_HARD_CAP",
    WriteDenialCode.AMOUNT_UNREADABLE: "BLOCKED_HARD_CAP",
    WriteDenialCode.FLOOR_EXCEEDED: "BLOCKED_HARD_CAP",
    WriteDenialCode.CEILING_EXCEEDED: "BLOCKED_HARD_CAP",
    WriteDenialCode.MAX_STEP_EXCEEDED: "BLOCKED_HARD_CAP",
    WriteDenialCode.DAILY_CAP_EXCEEDED: "BLOCKED_HARD_CAP",
    WriteDenialCode.MONTHLY_CAP_EXCEEDED: "BLOCKED_HARD_CAP",
    WriteDenialCode.STATE_DRIFT: "SKIPPED_DRIFT",
    WriteDenialCode.PACKAGE_BINDING_REQUIRED: "DENIED",
    WriteDenialCode.PACKAGE_BINDING_NOT_ALLOWED: "DENIED",
    WriteDenialCode.PACKAGE_APPROVAL_INVALID: "DENIED",
    WriteDenialCode.PACKAGE_APPROVAL_EXPIRED: "DENIED",
    WriteDenialCode.PACKAGE_CHANGED: "DENIED",
    WriteDenialCode.PACKAGE_BINDING_NOT_DERIVABLE: "DENIED",
    WriteDenialCode.PACKAGE_PARENT_UNCONFIRMED: "DENIED",
    WriteDenialCode.PACKAGE_SCOPE_MISMATCH: "DENIED",
    WriteDenialCode.PACKAGE_PAYLOAD_NOT_REPRODUCIBLE: "DENIED",
    WriteDenialCode.CREATIVE_CHECKSUM_MISMATCH: "DENIED",
}


def outcome_literal_for_denial(code: WriteDenialCode) -> _WriteVerdict:
    return _OUTCOME_BY_DENIAL[code]


class VerifierPort(Protocol):
    """Misma forma que `shared.crypto.ed25519.ApprovalVerifier.verify`."""

    def verify(self, payload: Mapping[str, JsonValue], signature: bytes) -> bool: ...


class AccountCapsPort(Protocol):
    """Misma forma que `broker.infrastructure.caps_config.AccountCaps`
    (tipado estructural: el dominio no importa infraestructura, solo
    declara que campos necesita)."""

    daily_cap_minor: int
    monthly_cap_minor: int
    floor_minor: int
    ceiling_minor: int
    max_step_pct: float
    max_changes_per_day: int


def recompute_diff_hash(intent: WriteIntent) -> str:
    """Comprobacion 3: recalcula desde `entity_ref`+`parametro`+valores
    vivos del `WriteIntent`. `valor_actual`/`valor_propuesto` ya llegan
    proyectados por `to_jsonable` (execution/infrastructure/broker_platform.py
    `_json`); `compute_diff_hash` vuelve a aplicar la misma proyeccion, que
    es idempotente sobre valores ya JSON-primitivos."""
    return compute_diff_hash(
        intent.entity_ref,
        intent.parametro,
        intent.valor_actual,
        intent.valor_propuesto,
        intent.managed_binding,
    )


def authorization_signing_payload(authorization: SignedAuthorization) -> Mapping[str, JsonValue]:
    """Los siete campos que el wire transporta (contracts/platform-port.md
    `SignedAuthorization`) -- el mismo conjunto que
    `proposals.domain.authorization.Authorization.signing_payload()` firma
    en `ads-api`: incluir `issued_by` en los dos lados es una decision
    deliberada (identifica quien decidio, parte del significado de la
    auditoria), no un descuido. `shared.crypto.ed25519.canonical_json` fija
    la serializacion; firmante y verificador deben construir el payload
    sobre exactamente estos campos, en este orden de claves (el
    `sort_keys=True` de `canonical_json` lo hace irrelevante en la
    practica, pero mismo conjunto de claves es obligatorio)."""
    return {
        **(
            {"managed_binding": cast(JsonValue, authorization.managed_binding.as_claims())}
            if authorization.managed_binding
            else {}
        ),
        "authorization_id": authorization.authorization_id,
        "proposal_id": authorization.proposal_id,
        "kind": authorization.kind,
        "diff_hash": authorization.diff_hash,
        "guardrail_verdict_hash": authorization.guardrail_verdict_hash,
        "issued_by": authorization.issued_by,
        "expires_at": authorization.expires_at.isoformat(),
    }


def verify_authorization(  # noqa: PLR0911 - independent fail-closed signature checks
    intent: WriteIntent,
    authorization: SignedAuthorization,
    live_diff_hash: str,
    now: datetime,
    verifier: VerifierPort,
) -> WriteDenialCode | None:
    """Comprobaciones 3-5 de contracts/platform-port.md, en orden: diff
    swap (T-60, intent y autorizacion deben citar el mismo `diff_hash`
    recomputado), caducidad, regla de `kind`, firma Ed25519. Fail closed:
    la primera discrepancia deniega, no se comprueban las siguientes."""
    if intent.managed_binding != authorization.managed_binding:
        return WriteDenialCode.MANAGED_BINDING_MISMATCH
    if intent.managed_binding is not None and intent.business_id != str(
        intent.managed_binding.account.business_id
    ):
        return WriteDenialCode.MANAGED_BINDING_MISMATCH
    if (
        live_diff_hash != intent.diff_hash
        or live_diff_hash != authorization.diff_hash
        or intent.diff_hash != authorization.diff_hash
    ):
        return WriteDenialCode.DIFF_HASH_MISMATCH
    if authorization.expires_at <= now:
        return WriteDenialCode.EXPIRED
    if authorization.kind == _RULE_AUTHORIZATION and _increases_spend(intent):
        return WriteDenialCode.RULE_AUTHORIZATION_CANNOT_INCREASE_SPEND
    try:
        signature = bytes.fromhex(authorization.signature)
    except ValueError:
        return WriteDenialCode.INVALID_SIGNATURE
    if not verifier.verify(authorization_signing_payload(authorization), signature):
        return WriteDenialCode.INVALID_SIGNATURE
    return None


def money_minor_units(value: JsonValue) -> int | None:
    """`valor_actual`/`valor_propuesto` de una operacion de presupuesto
    llegan como `proposals.domain.money.Money.to_canonical()`:
    `{"amount": "70.00", "currency": "..."}`. Cualquier otra forma (estado,
    lista de negativas...) no es dinero -- no cuenta para los topes de
    importe, solo para `max_changes_per_day` (que se aplica siempre).
    `creation_budget`/`_google` (T014, BL-5) es el unico lector del nativo
    de creacion: un plan que no valida por ese unico camino tampoco es
    dinero legible aqui -- `None`, nunca una excepcion (`check_hard_caps`
    lo convierte en denegacion, T-7)."""
    if isinstance(value, Mapping) and "creation_plan" in value:
        return _creation_minor_units(value)
    if not isinstance(value, Mapping) or "amount" not in value:
        return None
    amount = value["amount"]
    if not isinstance(amount, str):
        return None
    try:
        numeric = Decimal(amount)
        if not numeric.is_finite():
            return None
        return int((numeric * 100).to_integral_value())
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _creation_minor_units(value: Mapping[str, JsonValue]) -> int | None:
    """`creation_budget`/`_google` (T014, BL-5) es el unico lector del
    nativo de creacion; un plan que no valida por ese camino tampoco es
    dinero legible aqui -- `None`, nunca una excepcion (T-7)."""
    try:
        return int(creation_budget(value).amount * 100)
    except CampaignCreationError:
        return None


def _increases_spend(intent: WriteIntent) -> bool:
    """Threat-model.md T-60 "diff swap" + FR-11 (ninguna `rule_authorization`
    sube gasto): el sentido de la escritura se deriva de los valores
    (`valor_actual`/`valor_propuesto`), no del campo `operation` declarado
    por el cliente -- un `operation` mal etiquetado (p.ej. `LOWER_BUDGET`
    con valores que en realidad suben) no debe poder saltarse la regla.
    `intent.operation` sigue existiendo para que el adaptador sepa que
    llamada del SDK hacer, pero no participa en esta decision."""
    before = money_minor_units(intent.valor_actual)
    after = money_minor_units(intent.valor_propuesto)
    return before is not None and after is not None and after > before


def check_hard_caps(
    caps: AccountCapsPort,
    *,
    before_minor_units: int | None,
    after_minor_units: int | None,
    changes_count_today: int,
    applied_delta_today_minor_units: int,
    applied_delta_month_to_date_minor_units: int,
    is_creation: bool = False,
) -> WriteDenialCode | None:
    """Comprobacion 6: tope duro propio del broker (threat-model.md C-17),
    independiente del guardarraíl que ya evaluo `execution` (defensa en
    profundidad: aunque ese guardarraíl este mal calculado o la base de
    `ads-api` este comprometida, este tope se vuelve a aplicar aqui).
    Deniega, no recorta -- a diferencia de `GuardrailEvaluator._clamp`, el
    broker no tiene forma de proponerle a nadie un valor distinto.
    Una creacion (`is_creation=True`) con importe ilegible **nunca** cae en
    el `return None` generico (T-7, BL-5): es la unica red de dinero
    independiente de `ads-api` para PMax/Demand Gen/Display."""
    if changes_count_today >= caps.max_changes_per_day:
        return WriteDenialCode.MAX_CHANGES_PER_DAY
    if is_creation:
        return _check_creation_caps(
            caps,
            before_minor_units=before_minor_units,
            after_minor_units=after_minor_units,
            applied_delta_today_minor_units=applied_delta_today_minor_units,
            applied_delta_month_to_date_minor_units=applied_delta_month_to_date_minor_units,
        )
    if before_minor_units is None or after_minor_units is None:
        return None
    return _check_money_caps(
        caps,
        before_minor_units=before_minor_units,
        after_minor_units=after_minor_units,
        applied_delta_today_minor_units=applied_delta_today_minor_units,
        applied_delta_month_to_date_minor_units=applied_delta_month_to_date_minor_units,
    )


def _check_creation_caps(
    caps: AccountCapsPort,
    *,
    before_minor_units: int | None,
    after_minor_units: int | None,
    applied_delta_today_minor_units: int,
    applied_delta_month_to_date_minor_units: int,
) -> WriteDenialCode | None:
    """T-7 (BL-5): `after_minor_units is None` deniega, no permite --
    `creation_budget`/`_google` (T014) es el unico lector del nativo de
    creacion; si el no sabe leer el importe, ninguna otra funcion de este
    repo lo sabe tampoco."""
    if after_minor_units is None:
        return WriteDenialCode.AMOUNT_UNREADABLE
    if before_minor_units != 0 or after_minor_units < caps.floor_minor:
        return WriteDenialCode.FLOOR_EXCEEDED
    if after_minor_units > caps.ceiling_minor:
        return WriteDenialCode.CEILING_EXCEEDED
    return _check_spend_caps(
        caps,
        after_minor_units,
        applied_delta_today_minor_units=applied_delta_today_minor_units,
        applied_delta_month_to_date_minor_units=applied_delta_month_to_date_minor_units,
    )


def _check_money_caps(
    caps: AccountCapsPort,
    *,
    before_minor_units: int,
    after_minor_units: int,
    applied_delta_today_minor_units: int,
    applied_delta_month_to_date_minor_units: int,
) -> WriteDenialCode | None:
    bound_denial = _check_bounds(caps, before_minor_units, after_minor_units)
    if bound_denial is not None:
        return bound_denial
    delta = after_minor_units - before_minor_units
    if delta <= 0:
        return None
    return _check_spend_caps(
        caps,
        delta,
        applied_delta_today_minor_units=applied_delta_today_minor_units,
        applied_delta_month_to_date_minor_units=applied_delta_month_to_date_minor_units,
    )


def _check_bounds(
    caps: AccountCapsPort, before_minor_units: int, after_minor_units: int
) -> WriteDenialCode | None:
    if after_minor_units < caps.floor_minor:
        return WriteDenialCode.FLOOR_EXCEEDED
    if after_minor_units > caps.ceiling_minor:
        return WriteDenialCode.CEILING_EXCEEDED
    max_step_minor = round(before_minor_units * (caps.max_step_pct / 100))
    if abs(after_minor_units - before_minor_units) > max_step_minor:
        return WriteDenialCode.MAX_STEP_EXCEEDED
    return None


def _check_spend_caps(
    caps: AccountCapsPort,
    delta: int,
    *,
    applied_delta_today_minor_units: int,
    applied_delta_month_to_date_minor_units: int,
) -> WriteDenialCode | None:
    if applied_delta_today_minor_units + delta > caps.daily_cap_minor:
        return WriteDenialCode.DAILY_CAP_EXCEEDED
    if applied_delta_month_to_date_minor_units + delta > caps.monthly_cap_minor:
        return WriteDenialCode.MONTHLY_CAP_EXCEEDED
    return None


def check_state_drift(remote_state_hash: str, expected_state_hash: str) -> WriteDenialCode | None:
    """Comprobacion 7. `expected_state_hash` vacio (propuesta sin estado
    previo capturado, contracts/platform-port.md) nunca iguala un hash
    sha256 real -- se deniega por deriva, no se trata como "sin
    precondicion": las operaciones que este broker sabe mutar actuan
    siempre sobre una entidad ya existente."""
    return None if remote_state_hash == expected_state_hash else WriteDenialCode.STATE_DRIFT


def platform_account_id_from_google_resource_name(resource_name: str) -> str | None:
    """`customers/{customer_id}/...` -- el `customer_id` de Google Ads ya
    identifica la cuenta sin necesidad de leer nada remoto."""
    prefix, separator, rest = resource_name.partition("/")
    if prefix != "customers" or not separator:
        return None
    customer_id, _, _ = rest.partition("/")
    return customer_id or None


__all__ = [
    "AccountCapsPort",
    "VerifierPort",
    "WriteDenialCode",
    "authorization_signing_payload",
    "check_hard_caps",
    "check_state_drift",
    "money_minor_units",
    "outcome_literal_for_denial",
    "platform_account_id_from_google_resource_name",
    "recompute_diff_hash",
    "verify_authorization",
]
