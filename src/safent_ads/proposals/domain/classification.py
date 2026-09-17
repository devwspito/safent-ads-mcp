"""`Classification` y `ClassificationPolicy` (data-model.md invariante 4:
"`Clasificacion.IMPORTANTE` (todo `create_*`, subida de gasto, segmentacion,
publicar creatividad) exige autorizacion `human_approval`"; FR-12).

Anade `CRITICAL` sobre el `RUTINARIA|IMPORTANTE` de oposads: mismo criterio
de FR-12 por `ProposalKind`, mas un umbral de impacto en euros que escala
cualquier propuesta a critica independientemente del tipo (asuncion: el
`spec.md` no fija el umbral — se recibe como parametro de la politica, no
un magic number en el dominio)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from safent_ads.proposals.domain.money import Money


class Classification(StrEnum):
    ROUTINE = "routine"
    IMPORTANT = "important"
    CRITICAL = "critical"


class ProposalKind(StrEnum):
    BUDGET_INCREASE = "budget_increase"
    BUDGET_DECREASE = "budget_decrease"
    PAUSE = "pause"
    RESUME = "resume"
    BID_TARGET = "bid_target"
    NEGATIVE_KEYWORD = "negative_keyword"
    ROTATE_OUT_CREATIVE = "rotate_out_creative"
    TARGETING_CHANGE = "targeting_change"
    CREATIVE_PUBLICATION = "creative_publication"
    CREATE_CAMPAIGN = "create_campaign"
    CREATE_AD_SET = "create_ad_set"
    CREATE_AD = "create_ad"
    EXPERIMENT = "experiment"
    DELETE = "delete"
    # 004 tasks-2.md W3 (historia 20): `propose_native_write`. Carga libre
    # que el companion no sabe interpretar -- nunca autonoma (ver
    # `_ALWAYS_IMPORTANT`), el dueño la lee tal cual en el panel.
    NATIVE_WRITE = "native_write"


_ALWAYS_IMPORTANT: frozenset[ProposalKind] = frozenset(
    {
        ProposalKind.BUDGET_INCREASE,
        ProposalKind.TARGETING_CHANGE,
        ProposalKind.CREATIVE_PUBLICATION,
        ProposalKind.CREATE_CAMPAIGN,
        ProposalKind.CREATE_AD_SET,
        ProposalKind.CREATE_AD,
        # profitability-engine.md §4/§8: "propose_experiment... nunca corre
        # autonomo" -- un experimento nunca es RUTINARIA, exige aprobacion
        # humana igual que crear una campana.
        ProposalKind.EXPERIMENT,
        # design.md §0.7: borrar es irreversible
        # (nunca se puede deshacer, `UndoGracePolicy.grace_for`) -- nunca
        # RUTINARIA, igual que crear.
        ProposalKind.DELETE,
        # 004 tasks-2.md W2: reanudar reabre el gasto -- nunca RUTINARIA,
        # aprobacion humana como minimo.
        ProposalKind.RESUME,
        # 004 tasks-2.md W3/S-2 (security-engineer): carga libre que el
        # companion no interpreta -- ninguna regla AUTO puede autorizarla.
        ProposalKind.NATIVE_WRITE,
    }
)


@dataclass(frozen=True, slots=True)
class ClassificationPolicy:
    """FR-12 mas un techo de impacto que siempre escala a `CRITICAL`."""

    critical_impact_threshold: Money

    def classify(self, kind: ProposalKind, estimated_impact: Money) -> Classification:
        is_critical = estimated_impact.currency == self.critical_impact_threshold.currency and (
            estimated_impact.amount.copy_abs() >= self.critical_impact_threshold.amount.copy_abs()
        )
        if is_critical:
            return Classification.CRITICAL
        if kind in _ALWAYS_IMPORTANT:
            return Classification.IMPORTANT
        return Classification.ROUTINE
