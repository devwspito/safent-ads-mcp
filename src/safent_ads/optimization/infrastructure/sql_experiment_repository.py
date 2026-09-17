"""`ExperimentRepository` real sobre `experiments` (0026_experiments).

**Assumption documentada, escalada a `tech-lead`**: `randomization_unit`/
`arms` son columnas de `experiments` pensadas para geo-experimentos y
holdouts (§4), pero ni `design_experiment` ni `propose_experiment` (T201)
piden todavia esa configuracion -- el alcance de este lote es el caso mas
simple, A/B de campana con control/tratamiento. Se graban valores fijos
(`campaign`, `["control", "treatment"]`) hasta que una lane de creatividad/
plataforma anada el resto del diseno de aleatorizacion."""

from __future__ import annotations

import json
from typing import Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.optimization.application.dto import StoredExperiment
from safent_ads.optimization.domain.experiment import (
    Experiment,
    ExperimentState,
    SampleSizeDesign,
    minimum_duration_days,
)
from safent_ads.optimization.domain.identifiers import ExperimentId
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = ["SqlExperimentRepository"]

_DEFAULT_RANDOMIZATION_UNIT: Final = "campaign"
_DEFAULT_ARMS: Final = ["control", "treatment"]

_INSERT_EXPERIMENT: Final = """
    INSERT INTO experiments (id, business_id, entity_ref, hypothesis, metric,
                             randomization_unit, arms, baseline_rate, relative_mde,
                             sample_per_arm, duration_days, minimum_duration_days,
                             state, proposal_id)
    VALUES (:id, :business_id, :entity_ref, :hypothesis, :metric, :randomization_unit,
            CAST(:arms AS jsonb), :baseline_rate, :relative_mde, :sample_per_arm,
            :duration_days, :minimum_duration_days, :state, :proposal_id)
"""

_SELECT_EXPERIMENT: Final = """
    SELECT business_id, entity_ref, hypothesis, metric, baseline_rate, relative_mde,
           sample_per_arm, duration_days, state, proposal_id, result, created_at
      FROM experiments
     WHERE id = :id
"""


class SqlExperimentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        experiment: Experiment,
        proposal_id: str,
    ) -> None:
        await self._session.execute(
            text(_INSERT_EXPERIMENT),
            {
                "id": experiment.experiment_id.value,
                "business_id": business_id.value,
                "entity_ref": str(entity_ref),
                "hypothesis": experiment.hypothesis,
                "metric": experiment.metric,
                "randomization_unit": _DEFAULT_RANDOMIZATION_UNIT,
                "arms": json.dumps(_DEFAULT_ARMS),
                "baseline_rate": experiment.design.baseline_rate,
                "relative_mde": experiment.design.relative_mde,
                "sample_per_arm": experiment.design.sample_per_arm,
                "duration_days": experiment.duration_days,
                "minimum_duration_days": minimum_duration_days(
                    metric_measures_business_conversion=False
                ),
                "state": experiment.state.value,
                "proposal_id": proposal_id,
            },
        )
        await self._session.flush()

    async def get(self, *, experiment_id: ExperimentId) -> StoredExperiment | None:
        row = (
            await self._session.execute(
                text(_SELECT_EXPERIMENT), {"id": experiment_id.value}
            )
        ).mappings().first()
        return None if row is None else _row_to_stored_experiment(row, experiment_id)


def _row_to_stored_experiment(row: RowMapping, experiment_id: ExperimentId) -> StoredExperiment:
    design = SampleSizeDesign(
        baseline_rate=float(row["baseline_rate"]),
        relative_mde=float(row["relative_mde"]),
        sample_per_arm=row["sample_per_arm"],
        available_units_per_arm_per_week=0.0,  # no se persiste: solo sirve para recalcular semanas
    )
    experiment = Experiment(
        experiment_id=experiment_id,
        hypothesis=row["hypothesis"],
        metric=row["metric"],
        design=design,
        duration_days=row["duration_days"],
        state=ExperimentState(row["state"]),
        created_at=row["created_at"],
    )
    return StoredExperiment(
        business_id=str(row["business_id"]),
        entity_ref=row["entity_ref"],
        experiment=experiment,
        proposal_id=str(row["proposal_id"]) if row["proposal_id"] is not None else None,
        result=row["result"],
    )
