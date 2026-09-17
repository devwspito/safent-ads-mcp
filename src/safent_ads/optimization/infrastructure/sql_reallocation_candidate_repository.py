"""`ReallocationCandidateRepository` real (profitability-engine.md §3):
candidatos elegibles para una `AllocationPlan` desde `ad_entities` (nivel
campana, activa, presupuesto diario conocido) mas su ultima
`MarginalEstimate` materializada (`marginal_estimates`, 0017_optimization).
Un `INNER JOIN`: sin estimacion no hay `mContribution` que comparar, no es
candidato (profitability-engine.md §3: "mover euro del menor mContribution
al mayor" exige los dos numeros).

`min_viable_daily_spend`: el esquema no tiene un suelo explicito por
entidad todavia (`ad_entities` no declara una columna para ello, y
componerlo desde `guardrails` exigiria la unificacion Money/`GuardrailPolicy`
que documenta `execution/infrastructure/sql_guardrail_sets.py` -- fuera de
alcance de esta rama). Se usa una fraccion conservadora del gasto actual
(`_MIN_VIABLE_SPEND_FLOOR_PCT`), mismo patron que `_CRITICAL_IMPACT_THRESHOLD`
en `composition/container.py`: constante de politica declarada, no un
numero mágico sin nombre. Assumption documentada, pendiente de confirmacion
del propietario."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.domain.allocation import AllocationCandidate
from safent_ads.optimization.domain.marginal import EstimationMethod, MarginalEstimate
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.signals.domain.gates import LearningStatus

__all__ = ["SqlReallocationCandidateRepository"]

_MIN_VIABLE_SPEND_FLOOR_PCT: Final = Decimal("0.20")

_LEARNING_STATUS_BY_STATE: Final[dict[str, LearningStatus]] = {
    "LEARNING": LearningStatus.LEARNING,
    "LEARNED": LearningStatus.SUCCESS,
    "NOT_APPLICABLE": LearningStatus.SUCCESS,
}

_LIST_CANDIDATES = text("""
    SELECT entity.entity_ref, entity.budget_amount_minor, entity.budget_currency,
           entity.learning_state,
           estimate.value, estimate.ci_low, estimate.ci_high, estimate.method,
           estimate.sample_size
      FROM ad_entities AS entity
      JOIN marginal_estimates AS estimate
        ON estimate.business_id = entity.business_id AND estimate.entity_ref = entity.entity_ref
     WHERE entity.business_id = :business_id
       AND entity.level = 'campaign'
       AND entity.status = 'ACTIVE'
       AND entity.is_controllable
       AND entity.budget_amount_minor IS NOT NULL
     ORDER BY entity.entity_ref
""")


class SqlReallocationCandidateRepository:
    """`optimization.application.ports.ReallocationCandidateRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_candidates(self, *, business_id: BusinessId) -> tuple[AllocationCandidate, ...]:
        result = await self._session.execute(_LIST_CANDIDATES, {"business_id": business_id.value})
        return tuple(_to_candidate(row) for row in result.mappings())


def _to_candidate(row: RowMapping) -> AllocationCandidate:
    currency = str(row["budget_currency"])
    current = _money_from_minor(int(row["budget_amount_minor"]), currency)
    return AllocationCandidate(
        entity_ref=EntityRef.parse(str(row["entity_ref"])),
        current_daily_spend=current,
        min_viable_daily_spend=Money.of(current.amount * _MIN_VIABLE_SPEND_FLOOR_PCT, currency),
        marginal_estimate=MarginalEstimate(
            value=float(row["value"]),
            ci_low=float(row["ci_low"]),
            ci_high=float(row["ci_high"]),
            method=EstimationMethod(row["method"]),
            sample_size=int(row["sample_size"]),
        ),
        learning_status=_LEARNING_STATUS_BY_STATE.get(
            str(row["learning_state"]), LearningStatus.LEARNING
        ),
    )


def _money_from_minor(minor_units: int, currency: str) -> Money:
    return Money.of(Decimal(minor_units) / Decimal(100), currency)
