"""`LagCurve` (profitability-engine.md §2): Kaplan-Meier sobre cohortes
diarias de leads con censura por la derecha -- una media simple infravalora
las cohortes recientes, que aun pueden convertir.

`F(d)` = fraccion acumulada convertida a los `d` dias; `maturity(age)` =
`F(age)/F(D_max)`; `median_lag_days` = primer dia en que `F` alcanza la
mitad de su valor final. Regla asimetrica: subir exige `maturity >= 0.60`;
bajar exige `maturity >= 0.30` *y* que el extremo optimista del intervalo
proyectado siga peor que el objetivo -- decidir contra el mejor escenario es
seguro, contra el peor no."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.economics.domain.errors import (
    InsufficientLagObservationsError,
    NoConvergedCurveError,
)
from safent_ads.economics.domain.statistics import poisson_confidence_interval

DEFAULT_D_MAX = 120
MIN_OBSERVATIONS_FOR_PHASE_CURVE = 100
_RAISE_MATURITY_FLOOR = 0.60
_LOWER_MATURITY_FLOOR = 0.30


@dataclass(frozen=True, slots=True)
class LagObservation:
    """Una cohorte-lead: `duration_days` es la edad en la que convirtio (si
    `converted`) o la edad actual observada (si sigue censurada)."""

    duration_days: int
    converted: bool

    def __post_init__(self) -> None:
        if self.duration_days < 0:
            raise ValueError(f"duration_days negativo: {self.duration_days}")


@dataclass(frozen=True, slots=True)
class LagCurve:
    """Curva `F(d)` como funcion escalon sobre `[0, d_max]`, mas su tamano
    muestral (`sample_size`) para que quien la use decida si basta para una
    curva por fase (`MIN_OBSERVATIONS_FOR_PHASE_CURVE`)."""

    d_max: int
    sample_size: int
    _cumulative_by_day: tuple[float, ...]  # F(0..d_max), longitud d_max+1

    @classmethod
    def from_persisted(
        cls, *, d_max: int, sample_size: int, cumulative_by_day: list[float] | tuple[float, ...]
    ) -> LagCurve:
        """Reconstruye una curva guardada (`lag_curve_snapshots.cumulative_by_day`)
        sin exponer el campo interno a `infrastructure/` (encapsulacion)."""
        return cls(
            d_max=d_max, sample_size=sample_size, _cumulative_by_day=tuple(cumulative_by_day)
        )

    def as_cumulative_sequence(self) -> tuple[float, ...]:
        """Forma persistible de `F(0..d_max)` (contrapartida de `from_persisted`)."""
        return self._cumulative_by_day

    def f(self, day: int) -> float:
        clamped = max(0, min(day, self.d_max))
        return self._cumulative_by_day[clamped]

    def f_max(self) -> float:
        return self._cumulative_by_day[self.d_max]

    def median_lag_days(self) -> int:
        target = 0.5 * self.f_max()
        if target <= 0:
            raise NoConvergedCurveError("F(D_max) == 0: ninguna conversion observada")
        for day, value in enumerate(self._cumulative_by_day):
            if value >= target:
                return day
        return self.d_max

    def maturity(self, age_days: int) -> float:
        f_max = self.f_max()
        if f_max <= 0:
            raise NoConvergedCurveError("F(D_max) == 0: ninguna conversion observada")
        return self.f(age_days) / f_max

    @classmethod
    def from_observations(
        cls, observations: list[LagObservation], *, d_max: int = DEFAULT_D_MAX
    ) -> LagCurve:
        durations = [min(obs.duration_days, d_max) for obs in observations]
        paired = zip(durations, observations, strict=True)
        event_days = sorted({d for d, obs in paired if obs.converted})
        survival = 1.0
        survival_by_event_day: dict[int, float] = {}
        for t in event_days:
            at_risk = sum(1 for d in durations if d >= t)
            events = sum(
                1
                for d, obs in zip(durations, observations, strict=True)
                if obs.converted and d == t
            )
            if at_risk > 0:
                survival *= 1 - events / at_risk
            survival_by_event_day[t] = survival
        cumulative = _step_function(survival_by_event_day, d_max)
        return cls(d_max=d_max, sample_size=len(observations), _cumulative_by_day=cumulative)


def _step_function(survival_by_event_day: dict[int, float], d_max: int) -> tuple[float, ...]:
    values: list[float] = []
    current_survival = 1.0
    for day in range(d_max + 1):
        if day in survival_by_event_day:
            current_survival = survival_by_event_day[day]
        values.append(1 - current_survival)
    return tuple(values)


def assert_sufficient_for_phase_curve(sample_size: int) -> None:
    if sample_size < MIN_OBSERVATIONS_FOR_PHASE_CURVE:
        raise InsufficientLagObservationsError(
            f"n={sample_size} < {MIN_OBSERVATIONS_FOR_PHASE_CURVE}: usar la curva agregada"
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class MedianLagResolution:
    """T157: el `median_lag_days` que de verdad alimenta `AttributionLagGate`
    -- nunca la constante `_DEFAULT_MEDIAN_LAG_DAYS` de `orchestration` sin
    decirlo. `is_calibrated` distingue un dato real de un relleno."""

    median_lag_days: int
    is_calibrated: bool
    sample_size: int


def resolve_median_lag_days(
    curve: LagCurve | None, *, default_median_lag_days: int
) -> MedianLagResolution:
    """Cae al valor por defecto documentado (`is_calibrated=False`) cuando
    no hay curva o `sample_size < MIN_OBSERVATIONS_FOR_PHASE_CURVE`
    (profitability-engine.md §2: 'curva por fase con n >= 100 leads')."""
    if curve is None or curve.sample_size < MIN_OBSERVATIONS_FOR_PHASE_CURVE:
        return MedianLagResolution(
            median_lag_days=default_median_lag_days,
            is_calibrated=False,
            sample_size=0 if curve is None else curve.sample_size,
        )
    try:
        median = curve.median_lag_days()
    except NoConvergedCurveError:
        return MedianLagResolution(
            median_lag_days=default_median_lag_days,
            is_calibrated=False,
            sample_size=curve.sample_size,
        )
    return MedianLagResolution(
        median_lag_days=median, is_calibrated=True, sample_size=curve.sample_size
    )


@dataclass(frozen=True, kw_only=True, slots=True)
class CohortProjection:
    """Cohorte en vuelo (profitability-engine.md §2): `projected = observed
    / maturity`, con intervalo de Poisson exacto sobre `observed` dividido
    por la madurez."""

    observed: int
    age_days: int
    maturity: float
    projected: float
    projected_low: float
    projected_high: float

    @classmethod
    def build(
        cls, *, curve: LagCurve, observed: int, age_days: int, confidence: float = 0.90
    ) -> CohortProjection:
        maturity = curve.maturity(age_days)
        if maturity <= 0:
            raise NoConvergedCurveError(f"maturity <= 0 en age_days={age_days}")
        low, high = poisson_confidence_interval(observed, confidence)
        return cls(
            observed=observed,
            age_days=age_days,
            maturity=maturity,
            projected=observed / maturity,
            projected_low=low / maturity,
            projected_high=high / maturity,
        )

    @property
    def can_raise(self) -> bool:
        """Subir exige madurez >= 0,60 (decidir contra el mejor escenario
        es seguro solo si la cohorte ya es mayoritariamente observable)."""
        return self.maturity >= _RAISE_MATURITY_FLOOR

    def can_lower(
        self, *, optimistic_cost_per_conversion: float, target_cost_per_conversion: float
    ) -> bool:
        """Bajar exige madurez >= 0,30 y que incluso el escenario optimista
        (mas conversiones proyectadas -> coste menor) siga peor que el
        objetivo: decidir contra el peor escenario no es seguro."""
        return (
            self.maturity >= _LOWER_MATURITY_FLOOR
            and optimistic_cost_per_conversion > target_cost_per_conversion
        )
