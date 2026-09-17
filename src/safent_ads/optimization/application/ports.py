"""Puertos de `optimization` (mismo patron que `economics/application/
ports.py`: los casos de uso declaran sus puertos, los adaptadores concretos
viven en `infrastructure/` o, cuando cruzan hacia `proposals` (N5, por
encima de `optimization` N4.5 en el grafo), en `composition/` -- este
contexto nunca importa `proposals`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.application.dto import (
    MmmFitResult,
    ReallocationProposalRefs,
    StoredExperiment,
    StoredResponseCurve,
)
from safent_ads.optimization.domain.allocation import AllocationCandidate, AllocationPlan
from safent_ads.optimization.domain.calibration import (
    AutonomyLevel,
    CalibrationAdjustment,
    OutcomeSource,
    SignalOutcome,
)
from safent_ads.optimization.domain.diagnosis import EntityDiagnosisMetrics
from safent_ads.optimization.domain.experiment import Experiment
from safent_ads.optimization.domain.experiment_evaluation import (
    QuasiExperimentInput,
    QuasiExperimentResult,
)
from safent_ads.optimization.domain.identifiers import ExperimentId
from safent_ads.optimization.domain.marginal import MarginalEstimate
from safent_ads.rules.domain.autonomy import ActionKind
from safent_ads.shared.ids import BusinessId, EntityRef


class MarginalEstimateRepository(Protocol):
    """Estimacion materializada por el ciclo de mantenimiento (mismo
    patron que `economics.application.ports.LagCurveRepository`: recalcular
    el bootstrap en cada lectura es caro)."""

    async def get_latest(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> MarginalEstimate | None: ...

    async def save(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        estimate: MarginalEstimate,
        computed_at: datetime,
    ) -> None: ...


class ReallocationCandidateRepository(Protocol):
    """Candidatos elegibles para una `AllocationPlan`: gasto actual, suelo
    minimo viable y su ultima `MarginalEstimate` (ya materializada)."""

    async def list_candidates(
        self, *, business_id: BusinessId
    ) -> tuple[AllocationCandidate, ...]: ...


class ResponseCurveRepository(Protocol):
    async def get_latest(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> StoredResponseCurve | None: ...


class DiagnosisMetricsPort(Protocol):
    """Traduccion de frontera: ensambla `EntityDiagnosisMetrics` desde
    `accounts`/`metrics`/`economics`/`signals` (anticorrupcion, este
    contexto no conoce sus tipos internos)."""

    async def get_metrics(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> EntityDiagnosisMetrics | None: ...


class ReallocationProposalPort(Protocol):
    """Puerto de salida hacia `proposals` (N5, por encima de `optimization`
    en el grafo): `optimization` declara lo que necesita, `composition`
    cablea el adaptador concreto sobre `proposals.application` -- nunca al
    reves, para no crear un ciclo (plan.md §4)."""

    async def raise_reallocation_proposals(
        self, *, business_id: BusinessId, plan: AllocationPlan
    ) -> ReallocationProposalRefs: ...


class ContributionMarginPort(Protocol):
    """Vista minima sobre `economics` que `optimization` necesita para
    traducir conversiones de negocio en euros -- no el perfil completo (ISP)."""

    async def get_contribution_margin_per_conversion(
        self, *, business_id: BusinessId, product_id: str
    ) -> Money | None: ...


class ResponseCurveFitterPort(Protocol):
    """Cartera — PyMC-Marketing (profitability-engine.md §3b, ≥52 semanas):
    da `dE/dS` correcta por canal. El adaptador concreto
    (`infrastructure.pymc_budget_optimizer.PymcResponseCurveFitter`) es el
    unico lugar que importa `pymc_marketing`; si no converge o hay menos de
    52 semanas de historico, levanta `ResponseCurveFitUnavailableError` y
    quien orquesta cae al estimador tactico (a) -- degradacion documentada
    en el adaptador, no en este puerto."""

    def fit(
        self,
        *,
        weekly_spend: Any,  # noqa: ANN401 - `pandas.DataFrame`, ver adaptador concreto
        weekly_target: Any,  # noqa: ANN401 - `pandas.Series`
        channel_columns: list[str],
        control_columns: list[str] | None = None,
    ) -> MmmFitResult: ...


class BudgetOptimizerPort(Protocol):
    """`BudgetOptimizer.allocate_budget()` de PyMC-Marketing sobre un
    modelo ya ajustado (§3b)."""

    def optimize(
        self,
        *,
        fitted_model: Any,  # noqa: ANN401 - modelo pymc ya ajustado
        start_date: str,
        end_date: str,
        total_budget: float,
        budget_bounds: dict[str, tuple[float, float]] | None = None,
    ) -> dict[str, float]: ...


# --- T199: bucle de resultado a 14 dias (§6) --------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class DueSignalOutcome:
    """Una senal accionable que ya cruzo su horizonte de evaluacion y
    todavia no tiene fila en `signal_outcomes` (la condicion de "debida" la
    resuelve el puerto, no este modulo)."""

    signal_id: str
    business_id: BusinessId
    account_id: str
    entity_ref: EntityRef
    rule_code: str
    emitted_at: datetime


class PendingSignalOutcomesPort(Protocol):
    """Puerto hacia `signals`: senales accionables de un negocio cuya
    ventana ya cerro y aun no tienen `SignalOutcome` (anti-corrupcion,
    `optimization` no conoce el esquema de `signals`)."""

    async def list_due(
        self, *, business_id: BusinessId, cutoff: datetime
    ) -> tuple[DueSignalOutcome, ...]: ...


class SignalResolutionPort(Protocol):
    """Puerto hacia `proposals`: que le paso de verdad a la senal --
    `applied` si la propuesta que enlaza se ejecuto, `rejected` si el
    propietario la rechazo, `expired` en cualquier otro caso (incluido "no
    hay propuesta enlazada", tambien honesto para una senal que jamas
    disparo una regla)."""

    async def resolve_source(self, *, signal_id: str) -> OutcomeSource: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class EntityCpaWindowSnapshot:
    """Agregados crudos de la ventana posterior a la senal -- la
    aplicacion, no el puerto, decide que significa "mala" a partir de
    esto (division por cero incluida)."""

    spend: float
    leads: int
    target_cpa: float
    window_days: int


class EntityCpaWindowPort(Protocol):
    """Puerto hacia `metrics`/`accounts`: gasto y leads reales de la
    ventana, mas el mismo `target_cpa` que uso el motor de reglas para
    disparar la senal (`ad_entities.bid_target`, unica fuente real hoy --
    `orchestration.infrastructure.live_steps.LiveSignalStep`)."""

    async def get_snapshot(
        self, *, entity_ref: EntityRef, window_start: date, window_end: date
    ) -> EntityCpaWindowSnapshot | None: ...


class RuleActionKindPort(Protocol):
    """Puerto hacia `rules`: la accion que declara el catalogo para un
    `rule_code` -- decide si la senal "sube gasto" o avisa de un
    problema (`signal_outcome_evaluation.resolve_correctness`)."""

    async def get_action_kind(self, *, rule_code: str) -> ActionKind | None: ...


class SignalOutcomeRepository(Protocol):
    """Persiste un `SignalOutcome` (tabla `signal_outcomes`, UNIQUE por
    `signal_id`: idempotente) y, solo si `was_correct` no es `None`,
    materializa `signals.outcome_at_14d` para el panel/MCP que ya lo leen
    (nunca escribe `confirmed`/`not_confirmed` inventado)."""

    async def record(self, *, outcome: SignalOutcome) -> None: ...


# --- T200: bucle de calibracion (§6) -----------------------------------------


class CalibrationInputPort(Protocol):
    """Puerto hacia `signal_outcomes`: que reglas tienen contraste
    acumulado y sus `SignalOutcome` completos (para
    `calibration.compute_precision`, que exige la tupla, no un agregado ya
    reducido -- necesita distinguir `was_correct is None`)."""

    async def list_rule_codes_with_outcomes(self) -> tuple[str, ...]: ...

    async def list_outcomes_for_rule(self, *, rule_code: str) -> tuple[SignalOutcome, ...]: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class RuleCalibrationState:
    """Lo que `RecalibrateRules` necesita leer de una regla del catalogo
    antes de decidir que umbral mover (§6: 'mas gasto minimo, recorte
    menor, cooldown mas largo')."""

    rule_code: str
    autonomy_level: AutonomyLevel
    magnitude_pct: float | None
    cooldown_minutes: float


class RuleCalibrationStatePort(Protocol):
    """Puerto hacia `rules`: `rules.condition`/`magnitude_pct`/
    `cooldown_minutes` son la fuente que `rule_evaluator` lee de verdad
    (`SqlRuleRepository.get_by_code`, no una copia en memoria) -- escribir
    aqui SI cambia el comportamiento en vivo del motor de reglas."""

    async def get_state(self, *, rule_code: str) -> RuleCalibrationState | None: ...

    async def apply_adjustment(
        self, *, rule_code: str, threshold_name: str, new_value: float
    ) -> None: ...


class CalibrationAdjustmentLogPort(Protocol):
    """Puerto hacia `calibration_adjustments` (bitacora) y `audit.
    decision_log` (una entrada por negocio que aporto contraste a este
    ajuste -- `decision_log.business_id` es NOT NULL, y `rules` es
    global-scope hoy: no hay un unico negocio dueno del ajuste, ver
    Assumption de `RecalibrateRules`)."""

    async def already_adjusted_this_week(
        self, *, rule_code: str, threshold_name: str, week_start: date
    ) -> bool: ...

    async def record_adjustment(
        self,
        *,
        adjustment: CalibrationAdjustment,
        sample_size: int,
        precision: float | None,
        week_start: date,
        business_ids: tuple[str, ...],
    ) -> None: ...


# --- T201: herramientas MCP de experimentos (§4/§8) --------------------------


class ExperimentProposalPort(Protocol):
    """Puerto de salida hacia `proposals` (N5, por encima de `optimization`
    en el grafo, plan.md §4): `propose_experiment` NUNCA corre autonomo
    (§4), el adaptador concreto siempre clasifica `ProposalKind.EXPERIMENT`
    (IMPORTANT, exige aprobacion humana).

    Levanta la `Proposal` Y persiste el `Experiment` en `draft` **en la
    misma transaccion** (mismo criterio que `ReallocationProposalPort`,
    que ata la propuesta al `AllocationPlan` en un unico metodo): una
    propuesta sin su experimento, o viceversa, no debe poder existir."""

    async def raise_experiment_proposal(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        experiment: Experiment,
    ) -> str: ...


class ExperimentRepository(Protocol):
    """Lectura del agregado `Experiment` ya persistido (tabla `experiments`,
    0026_experiments). La escritura inicial vive en `ExperimentProposalPort`
    (atada a la propuesta que la autoriza); este puerto es solo de lectura."""

    async def get(self, *, experiment_id: ExperimentId) -> StoredExperiment | None: ...


class ExperimentEvaluationPort(Protocol):
    """T198: evaluacion de un `quasi_experiment` terminado (apagar/encender
    antes-despues, §4). Sin I/O -- el unico adaptador de hoy
    (`DiffInDifferencesEvaluator`) es puro; el puerto existe para que un
    futuro adaptador sobre `tfcausalimpact` (no es dependencia todavia,
    ver docstring de `experiment_evaluation.py`) lo sustituya sin tocar el
    llamador."""

    def evaluate(self, inputs: QuasiExperimentInput) -> QuasiExperimentResult: ...

