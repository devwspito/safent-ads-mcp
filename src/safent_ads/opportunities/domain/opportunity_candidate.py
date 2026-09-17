"""`OpportunityCandidate` (tasks.md T113, FR-35): patron de demanda sin
cobertura, ya listo para convertirse en una `Proposal` `CREATE_CAMPAIGN`.
Puro: ordena por `expected_contribution_delta` y reparte segun el
presupuesto de atencion diario (NFR-11) sin tocar infraestructura.

`account_ref` es un `EntityRef` de nivel `ACCOUNT` (no `CAMPAIGN`): la
campana todavia no existe en la plataforma y por tanto no tiene su propio
`EntityRef` (`mcp/presentation/catalog.py` documentaba esto como el motivo
de no exponer `propose_campaign` -- la cuenta, que si existe, es el ambito
correcto para la `Proposal`)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from safent_ads.opportunities.domain.campaign_brief import CampaignBrief
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityRef

# NFR-11 ("<= 10 propuestas pendientes/dia en operacion estable") acotado a
# esta UNA fuente de evidencia (huecos de calendario); otras fuentes futuras
# (T113: geos con CPA probado, huecos de competencia) sumaran su propio cupo
# cuando existan -- ver Assumption en generate_opportunities.py.
DEFAULT_MAX_NEW_CANDIDATES_PER_DAY = 3

# `Proposal.diff.parameter`/`cause_type` de toda propuesta CREATE_CAMPAIGN
# nacida de este contexto (T113 y T114 comparten el mismo prefijo para que
# FR-20 -- una sola propuesta abierta por entidad y parametro -- deduplique
# igual venga del ciclo deterministico o de la tool MCP).
PROPOSAL_CAUSE_TYPE = "opportunity_candidate"
PROPOSAL_PARAMETER_PREFIX = "new_campaign:"

# `proposals.parameter` tiene un CHECK `char_length BETWEEN 1 AND 64`
# (0008_proposals) -- un `candidate_key` legible (hito + cuenta) no cabe, asi
# que el `parameter` de verdad es un hash corto y estable; el `candidate_key`
# legible sigue viajando completo en `evidence`/`cause_sentence`.
_PARAMETER_HASH_LENGTH = 48


def proposal_parameter_for(candidate_key: str) -> str:
    digest = hashlib.sha256(candidate_key.encode("utf-8")).hexdigest()[:_PARAMETER_HASH_LENGTH]
    return f"{PROPOSAL_PARAMETER_PREFIX}{digest}"


@dataclass(frozen=True, kw_only=True, slots=True)
class OpportunityCandidate:
    """`candidate_key` es estable entre ciclos: identifica la MISMA
    oportunidad para que FR-20 (una sola propuesta abierta por entidad y
    parametro) la actualice en vez de duplicarla -- viaja como `parameter`
    de la `Proposal` (tasks.md T113: 'deduplicated: one open proposal per
    candidate key')."""

    business_id: BusinessId
    candidate_key: str
    account_ref: EntityRef
    brief: CampaignBrief
    expected_contribution_delta: Money
    cause_sentence: str


def rank_by_expected_contribution(
    candidates: tuple[OpportunityCandidate, ...],
) -> tuple[OpportunityCandidate, ...]:
    """Tool-surface.md §3 'la cola cambia de eje': mismo criterio que
    `list_proposals`, `expected_contribution_delta DESC`."""
    return tuple(
        sorted(candidates, key=lambda c: c.expected_contribution_delta.amount, reverse=True)
    )


@dataclass(frozen=True, slots=True)
class AttentionBudgetSplit:
    accepted: tuple[OpportunityCandidate, ...]
    deferred: tuple[OpportunityCandidate, ...]


def split_by_attention_budget(
    ranked_candidates: tuple[OpportunityCandidate, ...], *, remaining_slots: int
) -> AttentionBudgetSplit:
    """NFR-11: como mucho `remaining_slots` candidatos NUEVOS se aceptan en
    esta vuelta -- el resto se pospone (`postponed_reason=attention_budget`).
    Quien orquesta ya descuenta lo aceptado hoy de `remaining_slots`."""
    capped = max(remaining_slots, 0)
    return AttentionBudgetSplit(
        accepted=ranked_candidates[:capped], deferred=ranked_candidates[capped:]
    )
