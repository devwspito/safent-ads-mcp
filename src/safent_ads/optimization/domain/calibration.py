"""Bucle de calibracion (profitability-engine.md §6): los umbrales del
catalogo dejan de ser constantes. Cada `Signal` accionable genera un
`SignalOutcome` a 14 dias; caducadas y rechazadas son el grupo de control
natural y son gratis -- hay que guardarlas.

**Guardarrail innegociable**: en una regla `AUTO` la calibracion es
monotona conservadora -- nunca lo contrario. El agregado `CalibrationAdjustment`
**rechaza** el ajuste que viole la direccion declarada, no lo recorta en
silencio (invariante que este modulo protege)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from safent_ads.optimization.domain.errors import (
    CalibrationDirectionViolationError,
    InsufficientCalibrationSampleError,
)
from safent_ads.optimization.domain.identifiers import CalibrationAdjustmentId, SignalOutcomeId

MIN_SAMPLE_TO_CALIBRATE = 20
MIN_SAMPLE_TO_LOOSEN = 40
CONSERVATIVE_STEP_FRACTION_OF_RANGE = 0.10
LOW_PRECISION_THRESHOLD = 0.70
HIGH_PRECISION_THRESHOLD = 0.90
DEFAULT_OUTCOME_HORIZON_DAYS = 14
BAD_ENTITY_CPA_MULTIPLE = 1.5
BAD_ENTITY_WINDOW_DAYS = 14


class OutcomeSource(StrEnum):
    APPLIED = "applied"
    EXPIRED = "expired"
    REJECTED = "rejected"


class ThresholdDirection(StrEnum):
    """La direccion en la que 'mas conservador' se mueve para un umbral
    dado -- declarada por el umbral, nunca inferida (§6: 'cada umbral
    declara more_conservative_direction')."""

    HIGHER_IS_MORE_CONSERVATIVE = "higher_is_more_conservative"
    LOWER_IS_MORE_CONSERVATIVE = "lower_is_more_conservative"


class AutonomyLevel(StrEnum):
    NOTIFY = "notify"
    AUTO = "auto"
    APPROVAL = "approval"


@dataclass(frozen=True, kw_only=True, slots=True)
class SignalOutcome:
    """Resultado a 14 dias de una senal accionable (§6, FR-9, SC-4)."""

    outcome_id: SignalOutcomeId
    business_id: str
    account_id: str
    rule_code: str
    signal_id: str
    outcome_source: OutcomeSource
    was_correct: bool | None  # None si aun no ha vencido el horizonte
    observed_at: datetime
    horizon_days: int = DEFAULT_OUTCOME_HORIZON_DAYS


@dataclass(frozen=True, kw_only=True, slots=True)
class PrecisionReport:
    business_id: str
    account_id: str
    rule_code: str
    sample_size: int
    precision: float | None  # None si sample_size == 0


def compute_precision(outcomes: tuple[SignalOutcome, ...]) -> PrecisionReport | None:
    if not outcomes:
        return None
    decided = [o for o in outcomes if o.was_correct is not None]
    first = outcomes[0]
    precision = None if not decided else sum(1 for o in decided if o.was_correct) / len(decided)
    return PrecisionReport(
        business_id=first.business_id,
        account_id=first.account_id,
        rule_code=first.rule_code,
        sample_size=len(decided),
        precision=precision,
    )


@dataclass(frozen=True, kw_only=True, slots=True)
class CalibrationRecommendation:
    """Recomendacion derivada de `PrecisionReport`, previa a construir el
    `CalibrationAdjustment` (que exige ademas conocer el nivel de
    autonomia y la direccion conservadora del umbral)."""

    should_tighten: bool
    should_loosen: bool
    reason: str


def recommend_calibration(report: PrecisionReport) -> CalibrationRecommendation:
    if report.sample_size < MIN_SAMPLE_TO_CALIBRATE or report.precision is None:
        return CalibrationRecommendation(
            should_tighten=False,
            should_loosen=False,
            reason=f"n={report.sample_size} < {MIN_SAMPLE_TO_CALIBRATE}: solo se muestra el dato",
        )
    if report.precision < LOW_PRECISION_THRESHOLD:
        return CalibrationRecommendation(
            should_tighten=True,
            should_loosen=False,
            reason=f"precision {report.precision:.2f} < {LOW_PRECISION_THRESHOLD}",
        )
    if report.precision > HIGH_PRECISION_THRESHOLD and report.sample_size >= MIN_SAMPLE_TO_LOOSEN:
        return CalibrationRecommendation(
            should_tighten=False,
            should_loosen=True,
            reason=(
                f"precision {report.precision:.2f} > {HIGH_PRECISION_THRESHOLD}, "
                f"n>={MIN_SAMPLE_TO_LOOSEN}"
            ),
        )
    return CalibrationRecommendation(
        should_tighten=False, should_loosen=False, reason="dentro de banda, sin ajuste"
    )


@dataclass(frozen=True, kw_only=True, slots=True)
class CalibrationAdjustment:
    """Un paso de ajuste de umbral (§6: 'un paso del 10% del rango hacia lo
    conservador'; agresivo solo si `autonomy_level != AUTO`)."""

    adjustment_id: CalibrationAdjustmentId
    rule_code: str
    threshold_name: str
    direction: ThresholdDirection
    previous_value: float
    new_value: float
    autonomy_level: AutonomyLevel
    created_at: datetime

    @property
    def moved_towards_conservative(self) -> bool:
        if self.direction is ThresholdDirection.HIGHER_IS_MORE_CONSERVATIVE:
            return self.new_value >= self.previous_value
        return self.new_value <= self.previous_value


def build_conservative_step(
    *,
    adjustment_id: CalibrationAdjustmentId,
    rule_code: str,
    threshold_name: str,
    direction: ThresholdDirection,
    current_value: float,
    threshold_range: tuple[float, float],
    now: datetime,
) -> CalibrationAdjustment:
    """Un paso del 10% del rango hacia lo conservador (§6). Siempre
    permitido en `AUTO`: nunca afloja."""
    low, high = threshold_range
    step = CONSERVATIVE_STEP_FRACTION_OF_RANGE * (high - low)
    delta = step if direction is ThresholdDirection.HIGHER_IS_MORE_CONSERVATIVE else -step
    new_value = min(max(current_value + delta, low), high)
    return CalibrationAdjustment(
        adjustment_id=adjustment_id,
        rule_code=rule_code,
        threshold_name=threshold_name,
        direction=direction,
        previous_value=current_value,
        new_value=new_value,
        autonomy_level=AutonomyLevel.AUTO,
        created_at=now,
    )


def build_aggressive_step(
    *,
    adjustment_id: CalibrationAdjustmentId,
    rule_code: str,
    threshold_name: str,
    direction: ThresholdDirection,
    current_value: float,
    threshold_range: tuple[float, float],
    autonomy_level: AutonomyLevel,
    now: datetime,
) -> CalibrationAdjustment:
    """Volver a ser agresivo exige `autonomy_level != AUTO` (§6: 'Volver a
    ser agresivo en una AUTO exige aprobacion' -- este constructor nunca
    produce un ajuste agresivo para AUTO; ver `assert_does_not_loosen_auto_rule`
    para el rechazo explicito si alguien lo intenta de todas formas)."""
    if autonomy_level is AutonomyLevel.AUTO:
        raise CalibrationDirectionViolationError(
            f"regla {rule_code} es AUTO: no se afloja sin aprobacion"
        )
    low, high = threshold_range
    step = CONSERVATIVE_STEP_FRACTION_OF_RANGE * (high - low)
    delta = -step if direction is ThresholdDirection.HIGHER_IS_MORE_CONSERVATIVE else step
    new_value = min(max(current_value + delta, low), high)
    return CalibrationAdjustment(
        adjustment_id=adjustment_id,
        rule_code=rule_code,
        threshold_name=threshold_name,
        direction=direction,
        previous_value=current_value,
        new_value=new_value,
        autonomy_level=autonomy_level,
        created_at=now,
    )


def assert_does_not_loosen_auto_rule(
    adjustment: CalibrationAdjustment, *, autonomy_level: AutonomyLevel
) -> None:
    """Invariante innegociable (§6): un ajuste sobre una regla `AUTO` que se
    mueva hacia lo agresivo se **rechaza**, no se recorta en silencio."""
    if autonomy_level is AutonomyLevel.AUTO and not adjustment.moved_towards_conservative:
        raise CalibrationDirectionViolationError(
            f"ajuste de {adjustment.rule_code}.{adjustment.threshold_name} se mueve hacia lo "
            f"agresivo en una regla AUTO: {adjustment.previous_value} -> {adjustment.new_value}"
        )


def assert_sufficient_sample(sample_size: int) -> None:
    if sample_size < MIN_SAMPLE_TO_CALIBRATE:
        raise InsufficientCalibrationSampleError(
            f"n={sample_size} < {MIN_SAMPLE_TO_CALIBRATE}: no se calibra"
        )


def outcome_horizon(
    *, metric_measures_business_conversion: bool, median_lag_days: int | None = None
) -> int:
    if metric_measures_business_conversion and median_lag_days is not None:
        return median_lag_days + DEFAULT_OUTCOME_HORIZON_DAYS
    return DEFAULT_OUTCOME_HORIZON_DAYS


def outcome_due_at(signal_raised_at: datetime, *, horizon_days: int) -> datetime:
    return signal_raised_at + timedelta(days=horizon_days)


def is_bad_entity(*, cpa: float, target_cpa: float, window_days: int) -> bool:
    """CPA > 1,5x objetivo durante dos semanas (§6: base del calculo de
    *recall* aproximado)."""
    return window_days >= BAD_ENTITY_WINDOW_DAYS and cpa > BAD_ENTITY_CPA_MULTIPLE * target_cpa
