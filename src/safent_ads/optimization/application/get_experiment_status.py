"""`GetExperimentStatus` (contracts/mcp-tools.md P2 `get_experiment_status`):
lectura del agregado `Experiment` ya persistido."""

from __future__ import annotations

from safent_ads.optimization.application.dto import ExperimentStatusView
from safent_ads.optimization.application.errors import ExperimentNotFoundError
from safent_ads.optimization.application.ports import ExperimentRepository
from safent_ads.optimization.application.propose_experiment import stored_experiment_to_view
from safent_ads.optimization.domain.identifiers import ExperimentId


class GetExperimentStatus:
    def __init__(self, experiments: ExperimentRepository) -> None:
        self._experiments = experiments

    async def execute(self, *, experiment_id: ExperimentId) -> ExperimentStatusView:
        stored = await self._experiments.get(experiment_id=experiment_id)
        if stored is None:
            raise ExperimentNotFoundError(str(experiment_id))
        return stored_experiment_to_view(stored)


__all__ = ["GetExperimentStatus"]
