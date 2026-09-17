"""`ReallocationProposalPort` real: entrega un `AllocationPlan` a
`proposals.application.propose_action.ProposeAction` -- **la misma pieza**
que usan MCP (`composition/mcp_write_adapter.py::propose_budget_change`) y
`RuleCycle` para levantar una propuesta, para que las dos bajadas/subidas de
presupuesto de una reasignacion nunca diverjan del resto de propuestas de
presupuesto del sistema. Dos llamadas independientes (FR-12: "dos
propuestas separadas, bajada AUTO, subida con aprobacion"), en la MISMA
transaccion que el resto del caso de uso de `optimization` para que el par
donante/receptor de `allocation_plans` (0017_optimization,
`SqlAllocationPlanRepository`, ya existente) quede anexado junto a las
propuestas que genero -- nunca una sin la otra.

Ubicacion (Assumption documentada, escalado a software-architect): el
puerto de salida hacia `proposals` (N5, por encima de `optimization` N4.5
en plan.md §4) esta declarado para vivir en `composition/` (ver docstring
de `optimization/application/ports.py::ReallocationProposalPort`). Esta
rama no tiene autorizado anadir ficheros nuevos en `composition/` (mount
unico via `optimization/presentation/rest.py`), y hoy `proposals` no
importa `optimization` en ningun punto (comprobado, sin ciclo real) -- el
adaptador concreto se cablea aqui, en `optimization/infrastructure/`, como
excepcion deliberada y reversible a esa nota, no como cambio de la regla."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.economics.domain.money import Money as OptimizationMoney
from safent_ads.optimization.application.dto import ReallocationProposalRefs
from safent_ads.optimization.domain.allocation import (
    AllocationDirection,
    AllocationPlan,
    AllocationStep,
)
from safent_ads.optimization.infrastructure.sql_repositories import SqlAllocationPlanRepository
from safent_ads.proposals.application.propose_action import (
    ProposeAction,
    ProposeActionCommand,
    ProposeActionResult,
)
from safent_ads.proposals.domain.cause import Cause
from safent_ads.proposals.domain.classification import ClassificationPolicy, ProposalKind
from safent_ads.proposals.domain.money import Money as ProposalsMoney
from safent_ads.proposals.domain.priority import ExpiryPolicy, Urgency
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

__all__ = ["SqlReallocationProposalPort"]

_BUDGET_PARAMETER = "daily_budget"
_CAUSE_TYPE = "optimization_reallocation"

# Umbral de `ClassificationPolicy` duplicado a proposito de
# `composition/container.py::_CRITICAL_IMPACT_THRESHOLD` (mismo valor:
# Assumption documentada, spec.md no lo fija): tres lineas iguales pesan
# menos que una dependencia de `composition/container.py` desde
# `optimization/infrastructure/`, que si invertiria el grafo.
_CRITICAL_IMPACT_THRESHOLD = ProposalsMoney.of("2000")


class SqlReallocationProposalPort:
    """`optimization.application.ports.ReallocationProposalPort`."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._propose_action = ProposeAction(
            proposals=SqlProposalRepository(session),
            classification_policy=ClassificationPolicy(
                critical_impact_threshold=_CRITICAL_IMPACT_THRESHOLD
            ),
            expiry_policy=ExpiryPolicy(),
            clock=clock,
        )
        self._plans = SqlAllocationPlanRepository(session)

    async def raise_reallocation_proposals(
        self, *, business_id: BusinessId, plan: AllocationPlan
    ) -> ReallocationProposalRefs:
        decrease = await self._raise_step(business_id, plan.donor_step)
        increase = await self._raise_step(business_id, plan.receiver_step)
        await self._plans.save(
            plan=plan,
            decrease_proposal_id=str(decrease.proposal_id),
            increase_proposal_id=str(increase.proposal_id),
        )
        return ReallocationProposalRefs(
            decrease_proposal_id=str(decrease.proposal_id),
            increase_proposal_id=str(increase.proposal_id),
        )

    async def _raise_step(
        self, business_id: BusinessId, step: AllocationStep
    ) -> ProposeActionResult:
        before = _to_proposals_money(step.current_daily_spend)
        after = _to_proposals_money(step.proposed_daily_spend)
        diff = ProposedDiff.build(
            entity_ref=step.entity_ref, parameter=_BUDGET_PARAMETER, before=before, after=after
        )
        kind = (
            ProposalKind.BUDGET_INCREASE
            if step.direction is AllocationDirection.INCREASE
            else ProposalKind.BUDGET_DECREASE
        )
        return await self._propose_action.execute(
            ProposeActionCommand(
                business_id=business_id,
                diff=diff,
                kind=kind,
                cause=Cause(text=f"Reasignacion de cartera equimarginal ({step.entity_ref})"),
                cause_type=_CAUSE_TYPE,
                evidence=(),
                estimated_impact=_impact(before, after),
                urgency=Urgency.RECOMMENDED,
            )
        )


def _to_proposals_money(amount: OptimizationMoney) -> ProposalsMoney:
    return ProposalsMoney(amount=amount.amount, currency=amount.currency)


def _impact(before: ProposalsMoney, after: ProposalsMoney) -> ProposalsMoney:
    delta: Decimal = (after.amount - before.amount).copy_abs()
    return ProposalsMoney(amount=delta, currency=before.currency)
