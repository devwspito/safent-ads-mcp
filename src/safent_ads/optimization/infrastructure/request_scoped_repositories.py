"""Cablea `OptimizationQueryService` a Postgres real: una sesion por
llamada via `session_factory`, mismo patron que
`brand.infrastructure.sql_brand_kit_repository.RequestScopedBrandKitRepository`/
`panel.infrastructure.sql_read_model.RequestScopedPanelReadPort` -- cada
adaptador de `optimization/infrastructure/*.py` esta atado a UNA
`AsyncSession`, y el router se construye UNA vez al arrancar la app
(`optimization/presentation/rest.py::build_optimization_router_over_sql`)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.economics.domain.money import Money
from safent_ads.economics.infrastructure.sql_repositories import SqlUnitEconomicsProfileRepository
from safent_ads.optimization.application.dto import (
    ReallocationProposalRefs,
    StoredExperiment,
    StoredResponseCurve,
)
from safent_ads.optimization.domain.allocation import AllocationCandidate, AllocationPlan
from safent_ads.optimization.domain.calibration import SignalOutcome
from safent_ads.optimization.domain.diagnosis import EntityDiagnosisMetrics
from safent_ads.optimization.domain.experiment import Experiment
from safent_ads.optimization.domain.identifiers import ExperimentId
from safent_ads.optimization.domain.marginal import MarginalEstimate
from safent_ads.optimization.infrastructure.sql_calibration_ports import SqlCalibrationInputPort
from safent_ads.optimization.infrastructure.sql_contribution_margin_port import (
    SqlContributionMarginPort,
)
from safent_ads.optimization.infrastructure.sql_diagnosis_metrics_port import (
    SqlDiagnosisMetricsPort,
)
from safent_ads.optimization.infrastructure.sql_experiment_proposal_port import (
    SqlExperimentProposalPort,
)
from safent_ads.optimization.infrastructure.sql_experiment_repository import (
    SqlExperimentRepository,
)
from safent_ads.optimization.infrastructure.sql_reallocation_candidate_repository import (
    SqlReallocationCandidateRepository,
)
from safent_ads.optimization.infrastructure.sql_reallocation_proposal_port import (
    SqlReallocationProposalPort,
)
from safent_ads.optimization.infrastructure.sql_repositories import (
    SqlMarginalEstimateRepository,
    SqlResponseCurveRepository,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = [
    "RequestScopedCalibrationInputs",
    "RequestScopedContributionMargins",
    "RequestScopedDiagnosisMetrics",
    "RequestScopedExperimentProposals",
    "RequestScopedExperimentRepository",
    "RequestScopedMarginalEstimates",
    "RequestScopedReallocationCandidates",
    "RequestScopedReallocationProposals",
    "RequestScopedResponseCurves",
]


class RequestScopedMarginalEstimates:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_latest(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> MarginalEstimate | None:
        async with self._session_factory() as session:
            return await SqlMarginalEstimateRepository(session).get_latest(
                business_id=business_id, entity_ref=entity_ref
            )

    async def save(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        estimate: MarginalEstimate,
        computed_at: datetime,
    ) -> None:
        async with self._session_factory() as session:
            await SqlMarginalEstimateRepository(session).save(
                business_id=business_id,
                entity_ref=entity_ref,
                estimate=estimate,
                computed_at=computed_at,
            )
            await session.commit()


class RequestScopedResponseCurves:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_latest(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> StoredResponseCurve | None:
        async with self._session_factory() as session:
            return await SqlResponseCurveRepository(session).get_latest(
                business_id=business_id, entity_ref=entity_ref
            )


class RequestScopedReallocationCandidates:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_candidates(self, *, business_id: BusinessId) -> tuple[AllocationCandidate, ...]:
        async with self._session_factory() as session:
            return await SqlReallocationCandidateRepository(session).list_candidates(
                business_id=business_id
            )


class RequestScopedDiagnosisMetrics:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def get_metrics(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> EntityDiagnosisMetrics | None:
        async with self._session_factory() as session:
            return await SqlDiagnosisMetricsPort(session, self._clock).get_metrics(
                business_id=business_id, entity_ref=entity_ref
            )


class RequestScopedContributionMargins:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def get_contribution_margin_per_conversion(
        self, *, business_id: BusinessId, product_id: str
    ) -> Money | None:
        async with self._session_factory() as session:
            profiles = SqlUnitEconomicsProfileRepository(session)
            return await SqlContributionMarginPort(
                profiles, self._clock
            ).get_contribution_margin_per_conversion(business_id=business_id, product_id=product_id)


class RequestScopedReallocationProposals:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def raise_reallocation_proposals(
        self, *, business_id: BusinessId, plan: AllocationPlan
    ) -> ReallocationProposalRefs:
        async with self._session_factory() as session:
            refs = await SqlReallocationProposalPort(
                session, self._clock
            ).raise_reallocation_proposals(business_id=business_id, plan=plan)
            await session.commit()
            return refs


class RequestScopedExperimentProposals:
    """Una sesion para levantar la propuesta Y guardar el `Experiment`
    `draft` (`SqlExperimentProposalPort` hace ambos escritos atomicos, ver
    su docstring): las dos filas nacen juntas o ninguna."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def raise_experiment_proposal(
        self, *, business_id: BusinessId, entity_ref: EntityRef, experiment: Experiment
    ) -> str:
        async with self._session_factory() as session:
            proposal_id = await SqlExperimentProposalPort(
                session, self._clock
            ).raise_experiment_proposal(
                business_id=business_id, entity_ref=entity_ref, experiment=experiment
            )
            await session.commit()
            return proposal_id


class RequestScopedExperimentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, *, experiment_id: ExperimentId) -> StoredExperiment | None:
        async with self._session_factory() as session:
            return await SqlExperimentRepository(session).get(experiment_id=experiment_id)


class RequestScopedCalibrationInputs:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_rule_codes_with_outcomes(self) -> tuple[str, ...]:
        async with self._session_factory() as session:
            return await SqlCalibrationInputPort(session).list_rule_codes_with_outcomes()

    async def list_outcomes_for_rule(self, *, rule_code: str) -> tuple[SignalOutcome, ...]:
        async with self._session_factory() as session:
            return await SqlCalibrationInputPort(session).list_outcomes_for_rule(
                rule_code=rule_code
            )
