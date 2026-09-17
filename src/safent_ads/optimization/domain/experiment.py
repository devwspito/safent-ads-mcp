"""`Experiment` (profitability-engine.md §4): agregado `draft -> running ->
stopped_{success|futility|guardrail} -> concluded`, con hipotesis,
aleatorizacion, metrica, MDE, muestra, duracion y paradas.

El calculo de muestra reutiliza `economics.domain.statistics.
sample_size_per_arm` (su docstring ya declara: 'optimization lo reutiliza
para el calculo de muestra' -- economics N2.5 < optimization N4.5, sin
ciclos, plan.md §4)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from safent_ads.economics.domain.statistics import inverse_normal_cdf, sample_size_per_arm
from safent_ads.optimization.domain.errors import (
    ImpossibleExperimentDesignError,
    InvalidExperimentTransitionError,
)
from safent_ads.optimization.domain.identifiers import ExperimentId

MAX_DURATION_DAYS = 28
MIN_DURATION_DAYS = 7
DEFAULT_ALPHA = 0.10
DEFAULT_POWER = 0.80
FUTILITY_CONDITIONAL_POWER_FLOOR = 0.20
GUARDRAIL_CPE_MULTIPLE = 2.0
GUARDRAIL_SPEND_MULTIPLE_NO_CONVERSION = 5.0


class ExperimentState(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    STOPPED_SUCCESS = "stopped_success"
    STOPPED_FUTILITY = "stopped_futility"
    STOPPED_GUARDRAIL = "stopped_guardrail"
    CONCLUDED = "concluded"


_TRANSITIONS: dict[ExperimentState, frozenset[ExperimentState]] = {
    ExperimentState.DRAFT: frozenset({ExperimentState.RUNNING}),
    ExperimentState.RUNNING: frozenset(
        {
            ExperimentState.STOPPED_SUCCESS,
            ExperimentState.STOPPED_FUTILITY,
            ExperimentState.STOPPED_GUARDRAIL,
        }
    ),
    ExperimentState.STOPPED_SUCCESS: frozenset({ExperimentState.CONCLUDED}),
    ExperimentState.STOPPED_FUTILITY: frozenset({ExperimentState.CONCLUDED}),
    ExperimentState.STOPPED_GUARDRAIL: frozenset({ExperimentState.CONCLUDED}),
    ExperimentState.CONCLUDED: frozenset(),
}


class PlatformCapability(StrEnum):
    """profitability-engine.md §4: lo que cada plataforma permite de
    verdad -- tabla de capacidades, no codigo condicional disperso."""

    GOOGLE_CAMPAIGN_EXPERIMENT_SEARCH_DISPLAY = "google_campaign_experiment_search_display"
    GOOGLE_GEO_EXPERIMENT = "google_geo_experiment"
    GOOGLE_CONVERSION_LIFT_YOUTUBE_DISPLAY = "google_conversion_lift_youtube_display"
    META_AB_NATIVE_PER_PERSON = "meta_ab_native_per_person"
    META_CONVERSION_LIFT_HOLDOUT = "meta_conversion_lift_holdout"
    QUASI_EXPERIMENT_BEFORE_AFTER = "quasi_experiment_before_after"


@dataclass(frozen=True, kw_only=True, slots=True)
class PlatformCapabilityEntry:
    capability: PlatformCapability
    isolates_creativity: bool
    requires_account_manager: bool
    caveat: str


PLATFORM_CAPABILITY_TABLE: tuple[PlatformCapabilityEntry, ...] = (
    PlatformCapabilityEntry(
        capability=PlatformCapability.GOOGLE_CAMPAIGN_EXPERIMENT_SEARCH_DISPLAY,
        isolates_creativity=False,
        requires_account_manager=False,
        caveat="50/50; PMax no permite aislar creatividad",
    ),
    PlatformCapabilityEntry(
        capability=PlatformCapability.GOOGLE_GEO_EXPERIMENT,
        isolates_creativity=False,
        requires_account_manager=False,
        caveat="geo-experimentos nativos",
    ),
    PlatformCapabilityEntry(
        capability=PlatformCapability.GOOGLE_CONVERSION_LIFT_YOUTUBE_DISPLAY,
        isolates_creativity=False,
        requires_account_manager=True,
        caveat="solo YouTube/Display; en busqueda no hay lift de usuario self-serve",
    ),
    PlatformCapabilityEntry(
        capability=PlatformCapability.META_AB_NATIVE_PER_PERSON,
        isolates_creativity=True,
        requires_account_manager=False,
        caveat="aleatoriza por persona",
    ),
    PlatformCapabilityEntry(
        capability=PlatformCapability.META_CONVERSION_LIFT_HOLDOUT,
        isolates_creativity=False,
        requires_account_manager=True,
        caveat="presupuesto minimo, ventanas >= 7 dias; Advantage+ no aisla creatividad",
    ),
    PlatformCapabilityEntry(
        capability=PlatformCapability.QUASI_EXPERIMENT_BEFORE_AFTER,
        isolates_creativity=False,
        requires_account_manager=False,
        caveat="apagar/encender no es experimento: entra degradado con tfcausalimpact",
    ),
)


@dataclass(frozen=True, kw_only=True, slots=True)
class SampleSizeDesign:
    baseline_rate: float
    relative_mde: float
    sample_per_arm: int
    available_units_per_arm_per_week: float

    @property
    def weeks_needed(self) -> float:
        if self.available_units_per_arm_per_week <= 0:
            return float("inf")
        return self.sample_per_arm / self.available_units_per_arm_per_week


def design_sample_size(
    *,
    baseline_rate: float,
    relative_mde: float,
    available_units_per_arm_per_week: float,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> SampleSizeDesign:
    per_arm = sample_size_per_arm(
        baseline_rate=baseline_rate, relative_mde=relative_mde, alpha=alpha, power=power
    )
    return SampleSizeDesign(
        baseline_rate=baseline_rate,
        relative_mde=relative_mde,
        sample_per_arm=per_arm,
        available_units_per_arm_per_week=available_units_per_arm_per_week,
    )


def assert_design_is_feasible(design: SampleSizeDesign) -> None:
    """`design_experiment` rechaza el diseno inviable, no lo arranca
    (§4): duracion > 28 dias o volumen insuficiente (sin unidades/semana no
    hay division posible, `weeks_needed` ya devuelve `inf`)."""
    duration_days = design.weeks_needed * 7
    if duration_days > MAX_DURATION_DAYS:
        raise ImpossibleExperimentDesignError(
            f"duracion estimada {duration_days:.1f}d > {MAX_DURATION_DAYS}d "
            f"({design.sample_per_arm} unidades/brazo a "
            f"{design.available_units_per_arm_per_week}/semana)"
        )


def minimum_duration_days(
    *, metric_measures_business_conversion: bool, median_lag_days: int | None = None
) -> int:
    """Suelo de duracion (§4): 'minimo 7 dias y, si la metrica es
    conversion de negocio, `median_lag_days + 7`'."""
    if metric_measures_business_conversion and median_lag_days is not None:
        return max(MIN_DURATION_DAYS, median_lag_days + 7)
    return MIN_DURATION_DAYS


def conditional_power_below_futility_floor(conditional_power: float) -> bool:
    """Parada de futilidad a mitad de muestra (§4): potencia condicional
    < 20% -> parar. Solo interina de futilidad, nunca de exito (no infla
    el error tipo I)."""
    return conditional_power < FUTILITY_CONDITIONAL_POWER_FLOOR


def guardrail_breach(
    *, treatment_cpe: float, target_cpe: float, treatment_conversions: int
) -> bool:
    """Parada de guardarrail (§4): tratamiento por encima de 2x
    `target_cpe` acumulado, o >= 5x sin ninguna conversion."""
    if treatment_cpe > GUARDRAIL_CPE_MULTIPLE * target_cpe:
        return True
    return treatment_conversions == 0 and treatment_cpe >= (
        GUARDRAIL_SPEND_MULTIPLE_NO_CONVERSION * target_cpe
    )


@dataclass(frozen=True, kw_only=True, slots=True)
class ArmResult:
    """Conteo observado de un brazo al completar la muestra (§4: 'exito
    solo al completar la muestra')."""

    conversions: int
    sample_size: int

    def __post_init__(self) -> None:
        if self.sample_size <= 0:
            raise ImpossibleExperimentDesignError(f"sample_size <= 0: {self.sample_size}")
        if not 0 <= self.conversions <= self.sample_size:
            raise ImpossibleExperimentDesignError(
                f"conversions {self.conversions} fuera de [0, {self.sample_size}]"
            )

    @property
    def rate(self) -> float:
        return self.conversions / self.sample_size


def success_stop_reached(
    *, treatment: ArmResult, control: ArmResult, alpha: float = DEFAULT_ALPHA
) -> bool:
    """Parada de exito (§4): 'exito solo al completar la muestra, con IC
    90% excluyendo el 0' -- IC de Wald de dos proporciones sobre la
    diferencia de tasas. Nunca se llama a mitad de muestra (esa es la
    parada de futilidad, `conditional_power_below_futility_floor`): esta
    funcion asume que ambos brazos ya completaron `sample_per_arm`."""
    diff = treatment.rate - control.rate
    variance = (
        treatment.rate * (1 - treatment.rate) / treatment.sample_size
        + control.rate * (1 - control.rate) / control.sample_size
    )
    if variance <= 0:
        return diff != 0
    margin = inverse_normal_cdf(1 - alpha / 2) * math.sqrt(variance)
    return diff - margin > 0 or diff + margin < 0


@dataclass(kw_only=True, slots=True)
class Experiment:
    experiment_id: ExperimentId
    hypothesis: str
    metric: str
    design: SampleSizeDesign
    duration_days: int
    state: ExperimentState = ExperimentState.DRAFT
    created_at: datetime
    _events: list[str] = field(default_factory=list)

    @classmethod
    def draft(
        cls,
        *,
        experiment_id: ExperimentId,
        hypothesis: str,
        metric: str,
        design: SampleSizeDesign,
        now: datetime,
        metric_measures_business_conversion: bool = False,
        median_lag_days: int | None = None,
    ) -> Experiment:
        assert_design_is_feasible(design)
        floor_days = minimum_duration_days(
            metric_measures_business_conversion=metric_measures_business_conversion,
            median_lag_days=median_lag_days,
        )
        duration_days = max(floor_days, round(design.weeks_needed * 7))
        return cls(
            experiment_id=experiment_id,
            hypothesis=hypothesis,
            metric=metric,
            design=design,
            duration_days=duration_days,
            created_at=now,
        )

    def start(self) -> None:
        self._transition_to(ExperimentState.RUNNING)

    def stop_for_success(self) -> None:
        self._transition_to(ExperimentState.STOPPED_SUCCESS)

    def stop_for_futility(self) -> None:
        self._transition_to(ExperimentState.STOPPED_FUTILITY)

    def stop_for_guardrail_breach(self) -> None:
        self._transition_to(ExperimentState.STOPPED_GUARDRAIL)

    def conclude(self) -> None:
        self._transition_to(ExperimentState.CONCLUDED)

    def _transition_to(self, target: ExperimentState) -> None:
        allowed = _TRANSITIONS.get(self.state, frozenset())
        if target not in allowed:
            raise InvalidExperimentTransitionError(f"transicion invalida {self.state} -> {target}")
        self.state = target
