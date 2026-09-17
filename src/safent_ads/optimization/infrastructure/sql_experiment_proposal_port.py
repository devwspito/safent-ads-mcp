"""`ExperimentProposalPort` real: entrega el diseno de un experimento a
`proposals.application.propose_action.ProposeAction` (misma pieza que usa
el resto del sistema para levantar una propuesta) Y persiste el
`Experiment` en `draft` (`SqlExperimentRepository`) en la MISMA sesion --
una propuesta de experimento sin su fila en `experiments`, o viceversa, no
debe poder existir (mismo criterio que `SqlReallocationProposalPort` ata
la propuesta al `AllocationPlan` en un unico metodo). `ProposalKind.
EXPERIMENT` esta en `_ALWAYS_IMPORTANT` (proposals/domain/classification.py):
nunca `ROUTINE`, siempre exige aprobacion humana (profitability-engine.md
§4: "propose_experiment... nunca corre autonomo").

Ubicacion: misma excepcion documentada que
`sql_reallocation_proposal_port.py` -- el puerto de salida hacia
`proposals` (N5, por encima de `optimization` N4.5) esta declarado para
`composition/`, pero esta rama no tiene autorizado anadir ficheros ahi;
`proposals` no importa `optimization` en ningun punto, sin ciclo real."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.optimization.domain.experiment import Experiment
from safent_ads.optimization.infrastructure.sql_experiment_repository import (
    SqlExperimentRepository,
)
from safent_ads.proposals.application.propose_action import ProposeAction, ProposeActionCommand
from safent_ads.proposals.domain.cause import Cause
from safent_ads.proposals.domain.classification import ClassificationPolicy, ProposalKind
from safent_ads.proposals.domain.money import Money as ProposalsMoney
from safent_ads.proposals.domain.priority import ExpiryPolicy, Urgency
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = ["SqlExperimentProposalPort"]

_EXPERIMENT_PARAMETER = "experiment_design"
_CAUSE_TYPE = "optimization_experiment"
_MAX_CAUSE_LENGTH = 280
# Mismo criterio documentado que `sql_reallocation_proposal_port.py`: tres
# lineas iguales pesan menos que una dependencia de
# `composition/container.py` desde `optimization/infrastructure/`.
_CRITICAL_IMPACT_THRESHOLD = ProposalsMoney.of("2000")
_NO_IMPACT_ESTIMATE = ProposalsMoney.zero()


class SqlExperimentProposalPort:
    """`optimization.application.ports.ExperimentProposalPort`."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._propose_action = ProposeAction(
            proposals=SqlProposalRepository(session),
            classification_policy=ClassificationPolicy(
                critical_impact_threshold=_CRITICAL_IMPACT_THRESHOLD
            ),
            expiry_policy=ExpiryPolicy(),
            clock=clock,
        )
        self._experiments = SqlExperimentRepository(session)

    async def raise_experiment_proposal(
        self, *, business_id: BusinessId, entity_ref: EntityRef, experiment: Experiment
    ) -> str:
        diff = ProposedDiff.build(
            entity_ref=entity_ref,
            parameter=_EXPERIMENT_PARAMETER,
            before=None,
            after=_design_payload(experiment),
        )
        result = await self._propose_action.execute(
            ProposeActionCommand(
                business_id=business_id,
                diff=diff,
                kind=ProposalKind.EXPERIMENT,
                cause=Cause(text=experiment.hypothesis[:_MAX_CAUSE_LENGTH]),
                cause_type=_CAUSE_TYPE,
                evidence=(),
                estimated_impact=_NO_IMPACT_ESTIMATE,
                urgency=Urgency.RECOMMENDED,
            )
        )
        proposal_id = str(result.proposal_id)
        await self._experiments.save(
            business_id=business_id,
            entity_ref=entity_ref,
            experiment=experiment,
            proposal_id=proposal_id,
        )
        return proposal_id


def _design_payload(experiment: Experiment) -> dict[str, object]:
    return {
        "hypothesis": experiment.hypothesis,
        "metric": experiment.metric,
        "baseline_rate": experiment.design.baseline_rate,
        "relative_mde": experiment.design.relative_mde,
        "sample_per_arm": experiment.design.sample_per_arm,
        "duration_days": experiment.duration_days,
    }
