"""Catalogo de acciones defensivas (FR-11: "autonomo SOLO defensivo") y el
precheck puro de `apply_defensive_action` (contracts/mcp-tools.md; T069).

`precheck_apply_defensive_action` es una funcion de decision, no un caso de
uso: recibe banderas ya resueltas por la capa de aplicacion (freno leido en
vivo, condicion de regla viva, frescura del dato, veredicto de
guardarrailes) y devuelve el primer codigo de error del contrato que
aplique, o `None` si puede proceder. No hace I/O — eso mantiene el dominio
libre de framework."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from safent_ads.execution.domain.guardrails import GuardrailVerdict
from safent_ads.proposals.domain.money import Money

MAX_LOWER_BUDGET_PCT = 0.30


class DefensiveActionKind(StrEnum):
    LOWER_BUDGET = "lower_budget"
    PAUSE = "pause"
    EXIT_TEST_AD = "exit_test_ad"
    ADD_NEGATIVE_KEYWORD = "add_negative_keyword"
    ROTATE_OUT_CREATIVE = "rotate_out_creative"
    RESUME_ON_LATE_CONVERSION = "resume_on_late_conversion"


class DefensiveActionDenialCode(StrEnum):
    """Subconjunto de los codigos de `contracts/mcp-tools.md` que aplican a
    `apply_defensive_action`."""

    RULE_NOT_APPLICABLE = "RULE_NOT_APPLICABLE"
    BRAKE_ENGAGED = "BRAKE_ENGAGED"
    STALE_DATA = "STALE_DATA"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    VALIDATION_ERROR = "VALIDATION_ERROR"


@dataclass(frozen=True, slots=True)
class DefensiveAction:
    """Un comando del catalogo con su magnitud, si aplica."""

    kind: DefensiveActionKind
    magnitude_pct: float | None = None

    def is_defensive(self, before: Money, after: Money) -> bool:
        """Prueba pura: nunca incrementa el gasto equivalente. Cualquier
        `after > before` (p. ej. `resume_on_late_conversion` restaurando por
        encima del valor actual) falla esta prueba a proposito — esa accion
        debe pasar por aprobacion humana, no por `apply_defensive_action`."""
        return after <= before

    def respects_catalog_ceiling(self) -> bool:
        """Techo estructural del catalogo (T069: "lower_budget ≤30% clamp"),
        distinto e independiente del `max_step_pct` de `GuardrailSet` — esto
        es lo que la propia definicion de la accion permite pedir, no lo que
        el guardarraíl del ambito permite aplicar."""
        if self.kind is not DefensiveActionKind.LOWER_BUDGET:
            return True
        if self.magnitude_pct is None:
            return False
        return 0 < self.magnitude_pct <= MAX_LOWER_BUDGET_PCT


def precheck_apply_defensive_action(
    action: DefensiveAction,
    before: Money,
    after: Money,
    *,
    brake_engaged: bool,
    rule_condition_is_live: bool,
    data_is_stale: bool,
    guardrail_verdict: GuardrailVerdict | None,
) -> DefensiveActionDenialCode | None:
    """Orden de precedencia (asuncion documentada: el contrato no lo fija
    explicitamente salvo que el freno gana siempre — plan.md §6 paso 2
    ocurre antes que cualquier otra comprobacion):
    1. `BRAKE_ENGAGED` — el freno para todo antes de mirar nada mas.
    2. `STALE_DATA` — sin dato fresco no se puede confiar en ninguna otra
       comprobacion.
    3. `RULE_NOT_APPLICABLE` — la condicion no esta disparando ahora mismo.
    4. `VALIDATION_ERROR` — la accion no es defensiva o excede el techo del
       catalogo.
    5. `GUARDRAIL_BLOCKED` — el veredicto de guardarrailes la rechaza."""
    if brake_engaged:
        return DefensiveActionDenialCode.BRAKE_ENGAGED
    if data_is_stale:
        return DefensiveActionDenialCode.STALE_DATA
    if not rule_condition_is_live:
        return DefensiveActionDenialCode.RULE_NOT_APPLICABLE
    if not action.is_defensive(before, after) or not action.respects_catalog_ceiling():
        return DefensiveActionDenialCode.VALIDATION_ERROR
    if guardrail_verdict is not None and not guardrail_verdict.allowed:
        return DefensiveActionDenialCode.GUARDRAIL_BLOCKED
    return None
