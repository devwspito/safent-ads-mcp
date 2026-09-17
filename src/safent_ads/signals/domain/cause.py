"""`Cause` (data-model.md §Signal: 'causa en una frase') y su plantilla en
espanol natural. Un enum tipado, nunca texto libre — la frase se renderiza a
partir de la evidencia numerica de la senal."""

from __future__ import annotations

from enum import StrEnum


class Cause(StrEnum):
    GATE_BLOCKED = "gate_blocked"
    INSIDE_TARGET_BAND = "inside_target_band"
    ROAS_BELOW_TARGET_SUSTAINED = "roas_below_target_sustained"
    CPA_ABOVE_TARGET_POST_LEARNING = "cpa_above_target_post_learning"
    ZERO_CONVERSIONS_SPEND_MULTIPLE = "zero_conversions_spend_multiple"
    FREQUENCY_HIGH_CTR_LOW = "frequency_high_ctr_low"
    CTR_DECLINE_VS_BASELINE = "ctr_decline_vs_baseline"
    HOOK_RATE_LOW = "hook_rate_low"
    HOOK_AND_HOLD_RATE_HIGH = "hook_and_hold_rate_high"
    LIMITED_BY_BUDGET_AT_TARGET = "limited_by_budget_at_target"
    CPA_ABOVE_TARGET = "cpa_above_target"
    SEARCH_TERM_NON_CONVERTING = "search_term_non_converting"
    DAILY_SPEND_SPIKE = "daily_spend_spike"


_SENTENCES: dict[Cause, str] = {
    Cause.GATE_BLOCKED: "Sin senal accionable: {gate_reason}.",
    Cause.INSIDE_TARGET_BAND: "Dentro de la banda objetivo en {span}; se mantiene.",
    Cause.ROAS_BELOW_TARGET_SUSTAINED: (
        "ROAS {actual:.2f} por debajo del objetivo {target:.2f} en 3 y 7 dias."
    ),
    Cause.CPA_ABOVE_TARGET_POST_LEARNING: (
        "CPA {actual:.2f} entre 1.5 y 2 veces el objetivo {target:.2f} en {span}, "
        "ya fuera de aprendizaje."
    ),
    Cause.ZERO_CONVERSIONS_SPEND_MULTIPLE: (
        "Cero conversiones en {span} con gasto de {actual:.2f}, "
        "{multiple:.1f} veces el CPA objetivo."
    ),
    Cause.FREQUENCY_HIGH_CTR_LOW: (
        "Frecuencia {frequency:.2f} y CTR {ctr:.2%} con {impressions} impresiones en {span}."
    ),
    Cause.CTR_DECLINE_VS_BASELINE: (
        "CTR {actual:.2%} cae {drop_pct:.0%} frente a la linea base de {baseline:.2%}."
    ),
    Cause.HOOK_RATE_LOW: "Hook rate {actual:.0%} por debajo del minimo del 20%.",
    Cause.HOOK_AND_HOLD_RATE_HIGH: (
        "Hook rate {hook_rate:.0%} y hold rate {hold_rate:.0%} por encima del umbral de escalado."
    ),
    Cause.LIMITED_BY_BUDGET_AT_TARGET: (
        "Limitada por presupuesto con {actual:.0%} de cuota perdida y en objetivo en {span}."
    ),
    Cause.CPA_ABOVE_TARGET: (
        "CPA {actual:.2f} un {excess_pct:.0%} por encima del objetivo {target:.2f}."
    ),
    Cause.SEARCH_TERM_NON_CONVERTING: (
        "Termino de busqueda sin conversiones tras {clicks} clics y coste {actual:.2f}."
    ),
    Cause.DAILY_SPEND_SPIKE: "Gasto de hoy {actual:.2f}, {multiple:.1f} veces la media diaria.",
}


def describe(cause: Cause, **evidence: object) -> str:
    return _SENTENCES[cause].format(**evidence)
