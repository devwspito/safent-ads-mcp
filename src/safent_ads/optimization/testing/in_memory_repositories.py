"""Dobles en memoria de los puertos de `optimization` (application/ports.py),
usados por los tests de los casos de uso y por `mcp`/`panel` hasta que la
integracion cablee los adaptadores reales (mismo patron que
`economics.testing.in_memory_repositories`)."""

from __future__ import annotations

from datetime import date, datetime

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.application.dto import (
    ReallocationProposalRefs,
    StoredExperiment,
    StoredResponseCurve,
)
from safent_ads.optimization.application.ports import (
    DueSignalOutcome,
    EntityCpaWindowSnapshot,
    RuleCalibrationState,
)
from safent_ads.optimization.domain.allocation import AllocationCandidate, AllocationPlan
from safent_ads.optimization.domain.calibration import (
    CalibrationAdjustment,
    OutcomeSource,
    SignalOutcome,
)
from safent_ads.optimization.domain.diagnosis import EntityDiagnosisMetrics
from safent_ads.optimization.domain.experiment import Experiment
from safent_ads.optimization.domain.identifiers import ExperimentId
from safent_ads.optimization.domain.marginal import MarginalEstimate
from safent_ads.rules.domain.autonomy import ActionKind
from safent_ads.shared.ids import BusinessId, EntityRef


class InMemoryMarginalEstimateRepository:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], MarginalEstimate] = {}

    def seed(
        self, *, business_id: BusinessId, entity_ref: EntityRef, estimate: MarginalEstimate
    ) -> None:
        self._by_key[(str(business_id), str(entity_ref))] = estimate

    async def get_latest(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> MarginalEstimate | None:
        return self._by_key.get((str(business_id), str(entity_ref)))

    async def save(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        estimate: MarginalEstimate,
        computed_at: datetime,  # noqa: ARG002 - forma exacta del puerto
    ) -> None:
        self._by_key[(str(business_id), str(entity_ref))] = estimate


class InMemoryReallocationCandidateRepository:
    def __init__(self) -> None:
        self._by_business: dict[str, list[AllocationCandidate]] = {}

    def seed(self, *, business_id: BusinessId, candidates: list[AllocationCandidate]) -> None:
        self._by_business[str(business_id)] = candidates

    async def list_candidates(self, *, business_id: BusinessId) -> tuple[AllocationCandidate, ...]:
        return tuple(self._by_business.get(str(business_id), []))


class InMemoryResponseCurveRepository:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], StoredResponseCurve] = {}

    def seed(
        self, *, business_id: BusinessId, entity_ref: EntityRef, record: StoredResponseCurve
    ) -> None:
        self._by_key[(str(business_id), str(entity_ref))] = record

    async def get_latest(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> StoredResponseCurve | None:
        return self._by_key.get((str(business_id), str(entity_ref)))


class InMemoryDiagnosisMetricsPort:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], EntityDiagnosisMetrics] = {}

    def seed(
        self, *, business_id: BusinessId, entity_ref: EntityRef, metrics: EntityDiagnosisMetrics
    ) -> None:
        self._by_key[(str(business_id), str(entity_ref))] = metrics

    async def get_metrics(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> EntityDiagnosisMetrics | None:
        return self._by_key.get((str(business_id), str(entity_ref)))


class InMemoryContributionMarginPort:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], Money] = {}

    def seed(self, *, business_id: BusinessId, product_id: str, contribution_margin: Money) -> None:
        self._by_key[(str(business_id), product_id)] = contribution_margin

    async def get_contribution_margin_per_conversion(
        self, *, business_id: BusinessId, product_id: str
    ) -> Money | None:
        return self._by_key.get((str(business_id), product_id))


class InMemoryReallocationProposalPort:
    def __init__(self) -> None:
        self.raised_plans: list[AllocationPlan] = []

    async def raise_reallocation_proposals(
        self,
        *,
        business_id: BusinessId,  # noqa: ARG002 - forma exacta del puerto
        plan: AllocationPlan,
    ) -> ReallocationProposalRefs:
        self.raised_plans.append(plan)
        return ReallocationProposalRefs(
            decrease_proposal_id=f"decrease-{plan.plan_id}",
            increase_proposal_id=f"increase-{plan.plan_id}",
        )


class InMemoryPendingSignalOutcomesPort:
    def __init__(self) -> None:
        self._by_business: dict[str, list[DueSignalOutcome]] = {}

    def seed(self, *, business_id: BusinessId, due: list[DueSignalOutcome]) -> None:
        self._by_business[str(business_id)] = due

    async def list_due(
        self, *, business_id: BusinessId, cutoff: datetime
    ) -> tuple[DueSignalOutcome, ...]:
        candidates = self._by_business.get(str(business_id), [])
        return tuple(c for c in candidates if c.emitted_at <= cutoff)


class InMemorySignalResolutionPort:
    def __init__(self) -> None:
        self._by_signal_id: dict[str, OutcomeSource] = {}

    def seed(self, *, signal_id: str, source: OutcomeSource) -> None:
        self._by_signal_id[signal_id] = source

    async def resolve_source(self, *, signal_id: str) -> OutcomeSource:
        return self._by_signal_id.get(signal_id, OutcomeSource.EXPIRED)


class InMemoryEntityCpaWindowPort:
    def __init__(self) -> None:
        self._by_entity_ref: dict[str, EntityCpaWindowSnapshot] = {}

    def seed(self, *, entity_ref: EntityRef, snapshot: EntityCpaWindowSnapshot) -> None:
        self._by_entity_ref[str(entity_ref)] = snapshot

    async def get_snapshot(
        self,
        *,
        entity_ref: EntityRef,
        window_start: date,  # noqa: ARG002 - forma exacta del puerto
        window_end: date,  # noqa: ARG002 - forma exacta del puerto
    ) -> EntityCpaWindowSnapshot | None:
        return self._by_entity_ref.get(str(entity_ref))


class InMemoryRuleActionKindPort:
    def __init__(self) -> None:
        self._by_rule_code: dict[str, ActionKind] = {}

    def seed(self, *, rule_code: str, action_kind: ActionKind) -> None:
        self._by_rule_code[rule_code] = action_kind

    async def get_action_kind(self, *, rule_code: str) -> ActionKind | None:
        return self._by_rule_code.get(rule_code)


class InMemorySignalOutcomeRepository:
    def __init__(self) -> None:
        self.recorded: list[SignalOutcome] = []

    async def record(self, *, outcome: SignalOutcome) -> None:
        self.recorded.append(outcome)


class InMemoryCalibrationInputPort:
    def __init__(self) -> None:
        self._by_rule_code: dict[str, list[SignalOutcome]] = {}

    def seed(self, *, rule_code: str, outcomes: list[SignalOutcome]) -> None:
        self._by_rule_code[rule_code] = outcomes

    async def list_rule_codes_with_outcomes(self) -> tuple[str, ...]:
        return tuple(self._by_rule_code)

    async def list_outcomes_for_rule(self, *, rule_code: str) -> tuple[SignalOutcome, ...]:
        return tuple(self._by_rule_code.get(rule_code, []))


class InMemoryRuleCalibrationStatePort:
    def __init__(self) -> None:
        self._by_rule_code: dict[str, RuleCalibrationState] = {}
        self.applied: list[tuple[str, str, float]] = []

    def seed(self, *, state: RuleCalibrationState) -> None:
        self._by_rule_code[state.rule_code] = state

    async def get_state(self, *, rule_code: str) -> RuleCalibrationState | None:
        return self._by_rule_code.get(rule_code)

    async def apply_adjustment(
        self, *, rule_code: str, threshold_name: str, new_value: float
    ) -> None:
        self.applied.append((rule_code, threshold_name, new_value))
        current = self._by_rule_code[rule_code]
        if threshold_name == "magnitude_pct":
            self._by_rule_code[rule_code] = RuleCalibrationState(
                rule_code=current.rule_code,
                autonomy_level=current.autonomy_level,
                magnitude_pct=new_value,
                cooldown_minutes=current.cooldown_minutes,
            )
        else:
            self._by_rule_code[rule_code] = RuleCalibrationState(
                rule_code=current.rule_code,
                autonomy_level=current.autonomy_level,
                magnitude_pct=current.magnitude_pct,
                cooldown_minutes=new_value,
            )


class InMemoryCalibrationAdjustmentLogPort:
    def __init__(self) -> None:
        self._done_this_week: set[tuple[str, str, date]] = set()
        self.recorded: list[CalibrationAdjustment] = []

    def mark_done(self, *, rule_code: str, threshold_name: str, week_start: date) -> None:
        self._done_this_week.add((rule_code, threshold_name, week_start))

    async def already_adjusted_this_week(
        self, *, rule_code: str, threshold_name: str, week_start: date
    ) -> bool:
        return (rule_code, threshold_name, week_start) in self._done_this_week

    async def record_adjustment(
        self,
        *,
        adjustment: CalibrationAdjustment,
        sample_size: int,  # noqa: ARG002 - forma exacta del puerto
        precision: float | None,  # noqa: ARG002 - forma exacta del puerto
        week_start: date,
        business_ids: tuple[str, ...],  # noqa: ARG002 - forma exacta del puerto
    ) -> None:
        self._done_this_week.add((adjustment.rule_code, adjustment.threshold_name, week_start))
        self.recorded.append(adjustment)


class InMemoryExperimentRepository:
    def __init__(self) -> None:
        self._by_id: dict[str, StoredExperiment] = {}

    async def save(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        experiment: Experiment,
        proposal_id: str,
    ) -> None:
        self._by_id[str(experiment.experiment_id)] = StoredExperiment(
            business_id=str(business_id),
            entity_ref=str(entity_ref),
            experiment=experiment,
            proposal_id=proposal_id,
            result=None,
        )

    async def get(self, *, experiment_id: ExperimentId) -> StoredExperiment | None:
        return self._by_id.get(str(experiment_id))


class InMemoryExperimentProposalPort:
    """Guarda tambien el `Experiment` en el `InMemoryExperimentRepository`
    inyectado -- mismo criterio atomico que `SqlExperimentProposalPort`
    (una sola escritura logica, propuesta + experimento juntos)."""

    def __init__(self, experiments: InMemoryExperimentRepository) -> None:
        self._experiments = experiments
        self.raised: list[dict[str, object]] = []
        self.next_proposal_id = "proposal-1"

    async def raise_experiment_proposal(
        self, *, business_id: BusinessId, entity_ref: EntityRef, experiment: Experiment
    ) -> str:
        self.raised.append(
            {"business_id": business_id, "entity_ref": entity_ref, "experiment": experiment}
        )
        proposal_id = self.next_proposal_id
        await self._experiments.save(
            business_id=business_id,
            entity_ref=entity_ref,
            experiment=experiment,
            proposal_id=proposal_id,
        )
        return proposal_id
