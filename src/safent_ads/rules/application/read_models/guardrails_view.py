"""Proyeccion de lectura de `GET /guardrails?scope_ref` (contracts/rest-
api.md §Reglas y guardarraíles): lado de lectura de `PUT
/guardrails/{account_ref}` (`composition/execution_rest.py::put_guardrail`).

Solo ambito `platform_account`: es el unico que persiste
`rules.infrastructure.SqlGuardrailRepository` (contrato, clarificacion del
backend-engineer de la rama anterior) -- `business`/`campaign` quedan fuera
a proposito, mismo criterio que `PUT`. Las dos consultas que reunen estas
filas (`SqlGuardrailViewReadPort`, I-1 revision final T130) viven en
`rules.infrastructure.read_models.guardrails_view`, detras de
`ports.GuardrailViewReadPort`: este modulo se queda solo con la proyeccion
pura (`GuardrailView`, `parse_scope_ref`, `row_to_guardrail_view`).

`GuardrailView.guardrail_id == account_ref` (`<platform>:<external_id>`),
sin el UUID real de la fila: el panel manda `guardrailId` como `{id}` de
`PUT /guardrails/{id}`, que solo sabe resolver por `account_ref` -- mismo
razonamiento que `rule_catalog_view.py::RuleView.rule_id`."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = [
    "GuardrailView",
    "InvalidScopeRefError",
    "parse_scope_ref",
    "row_to_guardrail_view",
]

_MINOR_UNITS_PER_MAJOR = 100


class InvalidScopeRefError(ValueError):
    """`scope_ref` no es ni un `business_id` (UUID) ni un `account_ref`
    (`<platform>:<external_id>`)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class GuardrailView:
    guardrail_id: str
    scope: str
    scope_label: str
    daily_cap: float
    monthly_cap: float
    budget_floor: float
    budget_ceiling: float
    max_step_pct: float
    max_changes_per_entity_per_day: int
    min_viable_spend: float
    currency: str


def parse_scope_ref(scope_ref: str) -> tuple[str, str]:
    """`("business", business_id)` o `("account", account_ref)`. Ninguno de
    los dos formatos es ambiguo: un `business_id` es un UUID sin `:`, un
    `account_ref` siempre lleva `<platform>:<external_id>`."""
    try:
        return "business", str(uuid.UUID(scope_ref))
    except ValueError:
        pass
    platform, separator, external_id = scope_ref.partition(":")
    if not separator or not external_id or platform not in {"google", "meta"}:
        raise InvalidScopeRefError(scope_ref)
    return "account", scope_ref


def row_to_guardrail_view(row: Mapping[str, Any]) -> GuardrailView:
    """Fila cruda (de `SqlGuardrailViewReadPort`, cualquier `Mapping`
    indexable por columna) -> `GuardrailView`. Publica -- es el unico punto
    de acoplo que necesita el adaptador SQL para no reimplementar el
    mapeo."""
    account_ref = row.get("account_ref") or f"{row['platform']}:{row['external_account_id']}"
    return GuardrailView(
        guardrail_id=account_ref,
        scope="platform_account",
        scope_label=f"{row['platform']} · {row['external_account_id']}",
        daily_cap=_to_major(row["daily_cap_minor"]),
        monthly_cap=_to_major(row["monthly_cap_minor"]),
        budget_floor=_to_major(row["budget_floor_minor"]),
        budget_ceiling=_to_major(row["budget_ceiling_minor"]),
        max_step_pct=float(row["max_step_pct"]),
        max_changes_per_entity_per_day=int(row["max_changes_per_entity_per_day"]),
        # `GuardrailPolicy` no tiene `min_viable_spend` todavia (contrato,
        # clarificacion backend-engineer): no persistido, siempre 0.
        min_viable_spend=0.0,
        currency=row["currency"],
    )


def _to_major(minor: int) -> float:
    return float(Decimal(minor) / _MINOR_UNITS_PER_MAJOR)
