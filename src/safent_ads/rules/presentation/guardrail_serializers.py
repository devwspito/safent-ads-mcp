"""Serializacion de `GuardrailPolicy` para `PUT /guardrails/{account_ref}`
(contracts/rest-api.md §Reglas y guardarraíles): parseo del cuerpo entrante
y las dos formas de salida (evento de auditoria, respuesta HTTP) que
`composition/execution_rest.py::put_guardrail` necesita.

Movido desde `composition/execution_rest.py` (I-2, revision final T130):
son funciones puras, sin `Container`/sesion -- no tenian por que vivir en
el composition root, que solo cablea. `rules.presentation` ya es el dueno
de `GET /guardrails` (`rest.py`); este modulo completa el lado de
escritura con el mismo criterio."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from safent_ads.iam.presentation.errors import ApiError
from safent_ads.rules.domain.errors import InvalidGuardrailPolicyError
from safent_ads.rules.domain.guardrail import GuardrailPolicy

__all__ = [
    "guardrail_policy_json",
    "guardrail_policy_payload",
    "parse_guardrail_policy",
]

_MINOR_UNITS_PER_UNIT = 100


def parse_guardrail_policy(body: dict[str, Any]) -> GuardrailPolicy:
    try:
        return GuardrailPolicy(
            daily_cap_minor=_to_minor(_require_number(body, "daily_cap")),
            monthly_cap_minor=_to_minor(_require_number(body, "monthly_cap")),
            floor_minor=_to_minor(_require_number(body, "budget_floor")),
            ceiling_minor=_to_minor(_require_number(body, "budget_ceiling")),
            max_step_pct=_require_number(body, "max_step_pct"),
            max_changes_per_day=int(_require_number(body, "max_changes_per_entity_per_day")),
        )
    except InvalidGuardrailPolicyError as exc:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc


def guardrail_policy_payload(policy: GuardrailPolicy) -> dict[str, Any]:
    return {
        "daily_cap_minor": policy.daily_cap_minor,
        "monthly_cap_minor": policy.monthly_cap_minor,
        "floor_minor": policy.floor_minor,
        "ceiling_minor": policy.ceiling_minor,
        "max_step_pct": policy.max_step_pct,
        "max_changes_per_day": policy.max_changes_per_day,
    }


def guardrail_policy_json(
    account_ref: str, policy: GuardrailPolicy, currency: str
) -> dict[str, Any]:
    return {
        "account_ref": account_ref,
        "daily_cap": {"amount": str(_from_minor(policy.daily_cap_minor)), "currency": currency},
        "monthly_cap": {
            "amount": str(_from_minor(policy.monthly_cap_minor)),
            "currency": currency,
        },
        "budget_floor": {"amount": str(_from_minor(policy.floor_minor)), "currency": currency},
        "budget_ceiling": {
            "amount": str(_from_minor(policy.ceiling_minor)),
            "currency": currency,
        },
        "max_step_pct": policy.max_step_pct,
        "max_changes_per_entity_per_day": policy.max_changes_per_day,
    }


def _require_number(body: dict[str, Any], key: str) -> float:
    value = body.get(key)
    if value is None:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=f"{key} requerido")
    try:
        return float(Decimal(str(value)))
    except InvalidOperation as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message=f"{key} invalido"
        ) from exc


def _to_minor(amount: float) -> int:
    return int(round(amount * _MINOR_UNITS_PER_UNIT))


def _from_minor(minor: int) -> Decimal:
    return Decimal(minor) / _MINOR_UNITS_PER_UNIT
