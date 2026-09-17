"""Vistas de lectura y DTOs de infraestructura de `optimization` (mismo
patron que `economics/application/dto.py`: forma propia de presentacion /
persistencia, no el agregado de dominio)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.domain.diagnosis import DiagnosisAction, DiagnosisNodeName
from safent_ads.optimization.domain.experiment import Experiment
from safent_ads.optimization.domain.response_curve import (
    CurveConfidence,
    HillCurve,
    ObservedSpendRange,
    PowerCurve,
)


@dataclass(frozen=True, kw_only=True, slots=True)
class MmmFitResult:
    """Salida de `ResponseCurveFitterPort.fit()` (profitability-engine.md
    §3b): una `HillCurve` por canal que convergio, mas el diagnostico de
    convergencia agregado."""

    curves_by_channel: dict[str, HillCurve]
    r_hat_max: float


@dataclass(frozen=True, kw_only=True, slots=True)
class StoredResponseCurve:
    """Forma persistida de una `ResponseCurve` (Hill o potencia,
    discriminadas por tipo concreto de `curve`)."""

    curve: HillCurve | PowerCurve
    observed_range: ObservedSpendRange
    residual_std: float
    curve_confidence: CurveConfidence


@dataclass(frozen=True, kw_only=True, slots=True)
class ReallocationProposalRefs:
    decrease_proposal_id: str
    increase_proposal_id: str


@dataclass(frozen=True, kw_only=True, slots=True)
class MarginalRoasView:
    entity_ref: str
    value: float
    ci_low: float
    ci_high: float
    method: str
    sample_size: int
    inconclusive: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class DiagnosisNodeView:
    order: int
    name: DiagnosisNodeName
    matched: bool
    action: DiagnosisAction | None
    reason: str
    reference_check_ids: tuple[str, ...]


@dataclass(frozen=True, kw_only=True, slots=True)
class DiagnosisView:
    entity_ref: str
    action: DiagnosisAction
    nodes: tuple[DiagnosisNodeView, ...]


@dataclass(frozen=True, kw_only=True, slots=True)
class SpendChangeSimulationView:
    entity_ref: str
    current_spend: float
    proposed_spend: float
    out_of_support: bool
    expected_contribution_delta: Money | None = None
    low_contribution_delta: Money | None = None
    high_contribution_delta: Money | None = None
    band_confidence: float | None = None
    curve_confidence: str | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class AllocationStepView:
    entity_ref: str
    direction: str
    current_daily_spend: Money
    proposed_daily_spend: Money
    step_pct: float
    requires_approval: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class ReallocationPlanView:
    donor: AllocationStepView
    receiver: AllocationStepView
    expected_contribution_delta: Money
    decrease_proposal_id: str
    increase_proposal_id: str


@dataclass(frozen=True, kw_only=True, slots=True)
class ExperimentDesignView:
    """Salida de `design_experiment` (profitability-engine.md §4/§8):
    `feasible=False` es un rechazo con motivo tipado, no una excepcion que
    cruce el limite de MCP."""

    feasible: bool
    sample_per_arm: int
    weeks_needed: float
    duration_days: int
    minimum_duration_days: int
    rejection_reason: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class StoredExperiment:
    """Forma persistida de un `Experiment` (tabla `experiments`,
    0026_experiments): el agregado de dominio mas lo que solo sabe la
    infraestructura (a que negocio/entidad pertenece, que propuesta lo
    autoriza, el resultado ya evaluado si lo hay)."""

    business_id: str
    entity_ref: str
    experiment: Experiment
    proposal_id: str | None
    result: dict[str, object] | None


@dataclass(frozen=True, kw_only=True, slots=True)
class ExperimentStatusView:
    experiment_id: str
    business_id: str
    entity_ref: str
    hypothesis: str
    metric: str
    state: str
    sample_per_arm: int
    duration_days: int
    proposal_id: str | None
    result: dict[str, object] | None


@dataclass(frozen=True, kw_only=True, slots=True)
class RulePrecisionView:
    rule_code: str
    sample_size: int
    precision: float | None
    recommendation: str


@dataclass(frozen=True, kw_only=True, slots=True)
class CalibrationReportView:
    rules: tuple[RulePrecisionView, ...]
