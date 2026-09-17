"""Clasificacion de correccion de una `Signal` accionable ya vencida
(profitability-engine.md §6, tasks.md T199): puro, sin I/O -- el llamador
(la aplicacion) ya resolvio `outcome_source`, si la entidad "salio mala"
(`calibration.is_bad_entity`) y la direccion de la regla que la disparo.

**Auto-confirmacion (§6, 'Fallos')**: bajo el gasto de una regla defensiva
(SELL/EXIT/...) ya aplicada, el CPA post-corte mejora casi siempre por
construccion -- confirmarla contra ese mismo CPA seria apuntarse el
acierto sin haberlo demostrado. Por eso una defensiva `APPLIED` es
`INCONCLUSIVE` aqui: el §6 pide contrastarla contra la contribucion
marginal, que este modulo no calcula. Las caducadas/rechazadas SI son
contrastables: son el grupo de control gratis que nunca recibio el
recorte."""

from __future__ import annotations

from enum import StrEnum

from safent_ads.optimization.domain.calibration import OutcomeSource
from safent_ads.rules.domain.autonomy import ActionKind, increases_spend


class OutcomeVerdict(StrEnum):
    """Etiqueta de presentacion (T199: 'CONFIRMED / CONTRADICTED /
    INCONCLUSIVE'). `SignalOutcome.was_correct` es la forma que consume
    `calibration.compute_precision`; este enum es solo para lo que se
    imprime/loguea."""

    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"


def verdict_of(was_correct: bool | None) -> OutcomeVerdict:
    if was_correct is None:
        return OutcomeVerdict.INCONCLUSIVE
    return OutcomeVerdict.CONFIRMED if was_correct else OutcomeVerdict.CONTRADICTED


def resolve_correctness(
    *,
    action_kind: ActionKind,
    outcome_source: OutcomeSource,
    entity_is_bad: bool | None,
) -> bool | None:
    """`None` (INCONCLUSIVE) siempre que falte dato o el caso sea el de
    auto-confirmacion documentado arriba -- nunca se inventa un
    confirmado/contradicho."""
    if entity_is_bad is None:
        return None
    if increases_spend(action_kind):
        return _correctness_for_spend_increasing_rule(outcome_source, entity_is_bad)
    return _correctness_for_defensive_rule(outcome_source, entity_is_bad)


def _correctness_for_spend_increasing_rule(
    outcome_source: OutcomeSource, entity_is_bad: bool
) -> bool | None:
    """La regla dijo 'escala, va bien'. Solo se puede contrastar si de
    verdad se escalo (aplicada): sin contrafactual de una oportunidad no
    aprovechada, caducada/rechazada quedan inconcluyentes."""
    if outcome_source is not OutcomeSource.APPLIED:
        return None
    return not entity_is_bad


def _correctness_for_defensive_rule(
    outcome_source: OutcomeSource, entity_is_bad: bool
) -> bool | None:
    """La regla dijo 'hay un problema, corta'. Aplicada: inconcluyente
    (auto-confirmacion, ver docstring del modulo). Caducada/rechazada: el
    aviso fue correcto si la entidad de verdad salio mala sin intervencion
    -- el contrafactual gratis que pide §6."""
    if outcome_source is OutcomeSource.APPLIED:
        return None
    return entity_is_bad
