"""`ProposeExperiment` (contracts/mcp-tools.md P2 `propose_experiment`,
profitability-engine.md §4): crea una `Proposal` de tipo experimento que
**exige aprobacion del propietario** -- nunca corre autonomo. Revalida la
viabilidad del diseno aqui, no se fia de lo que ya calculo `design_experiment`
del lado del llamador (defensa en profundidad, mismo criterio que
`AuthorizeRuleAction` re-verifica una regla `AUTO` en vivo)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.optimization.application.dto import ExperimentStatusView, StoredExperiment
from safent_ads.optimization.application.errors import ExperimentNotFoundError
from safent_ads.optimization.application.ports import ExperimentProposalPort, ExperimentRepository
from safent_ads.optimization.domain.experiment import (
    DEFAULT_ALPHA,
    DEFAULT_POWER,
    Experiment,
    assert_design_is_feasible,
    design_sample_size,
)
from safent_ads.optimization.domain.identifiers import ExperimentId
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityRef


@dataclass(frozen=True, kw_only=True, slots=True)
class ProposeExperimentRequest:
    business_id: BusinessId
    entity_ref: EntityRef
    hypothesis: str
    metric: str
    baseline_rate: float
    relative_mde: float
    available_units_per_arm_per_week: float
    metric_measures_business_conversion: bool = False
    median_lag_days: int | None = None


class ProposeExperiment:
    def __init__(
        self,
        *,
        proposals: ExperimentProposalPort,
        experiments: ExperimentRepository,
        clock: Clock,
    ) -> None:
        self._proposals = proposals
        self._experiments = experiments
        self._clock = clock

    async def execute(self, request: ProposeExperimentRequest) -> ExperimentStatusView:
        design = design_sample_size(
            baseline_rate=request.baseline_rate,
            relative_mde=request.relative_mde,
            available_units_per_arm_per_week=request.available_units_per_arm_per_week,
            alpha=DEFAULT_ALPHA,
            power=DEFAULT_POWER,
        )
        assert_design_is_feasible(design)  # ImpossibleExperimentDesignError si no -- MCP la traduce
        experiment = Experiment.draft(
            experiment_id=ExperimentId.new(),
            hypothesis=request.hypothesis,
            metric=request.metric,
            design=design,
            now=self._clock.now(),
            metric_measures_business_conversion=request.metric_measures_business_conversion,
            median_lag_days=request.median_lag_days,
        )
        await self._proposals.raise_experiment_proposal(
            business_id=request.business_id, entity_ref=request.entity_ref, experiment=experiment
        )
        stored = await self._experiments.get(experiment_id=experiment.experiment_id)
        if stored is None:  # pragma: no cover - se acaba de guardar en la misma transaccion
            raise ExperimentNotFoundError(str(experiment.experiment_id))
        return stored_experiment_to_view(stored)


def stored_experiment_to_view(stored: StoredExperiment) -> ExperimentStatusView:
    """Compartido con `GetExperimentStatus`: misma proyeccion de lectura,
    un unico lugar que la mantiene."""
    experiment = stored.experiment
    return ExperimentStatusView(
        experiment_id=str(experiment.experiment_id),
        business_id=stored.business_id,
        entity_ref=stored.entity_ref,
        hypothesis=experiment.hypothesis,
        metric=experiment.metric,
        state=experiment.state.value,
        sample_per_arm=experiment.design.sample_per_arm,
        duration_days=experiment.duration_days,
        proposal_id=stored.proposal_id,
        result=stored.result,
    )


__all__ = ["ProposeExperiment", "ProposeExperimentRequest", "stored_experiment_to_view"]
