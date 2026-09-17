"""`DesignExperiment` (contracts/mcp-tools.md P2 `design_experiment`,
profitability-engine.md §4): viabilidad, muestra, dias y limites. Puro --
sin puertos, sin I/O -- `design_sample_size`/`assert_design_is_feasible` ya
son la unica verdad; esto solo traduce el rechazo tipado a una vista, nunca
deja que `ImpossibleExperimentDesignError` cruce el limite de MCP como una
excepcion sin forma (regla 3 del contrato: solo lectura o propuesta, nunca
un fallo opaco)."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.optimization.application.dto import ExperimentDesignView
from safent_ads.optimization.domain.errors import ImpossibleExperimentDesignError
from safent_ads.optimization.domain.experiment import (
    DEFAULT_ALPHA,
    DEFAULT_POWER,
    SampleSizeDesign,
    assert_design_is_feasible,
    design_sample_size,
    minimum_duration_days,
)


@dataclass(frozen=True, kw_only=True, slots=True)
class DesignExperimentRequest:
    baseline_rate: float
    relative_mde: float
    available_units_per_arm_per_week: float
    metric_measures_business_conversion: bool = False
    median_lag_days: int | None = None
    alpha: float = DEFAULT_ALPHA
    power: float = DEFAULT_POWER


class DesignExperiment:
    def execute(self, request: DesignExperimentRequest) -> ExperimentDesignView:
        design = design_sample_size(
            baseline_rate=request.baseline_rate,
            relative_mde=request.relative_mde,
            available_units_per_arm_per_week=request.available_units_per_arm_per_week,
            alpha=request.alpha,
            power=request.power,
        )
        floor_days = minimum_duration_days(
            metric_measures_business_conversion=request.metric_measures_business_conversion,
            median_lag_days=request.median_lag_days,
        )
        try:
            assert_design_is_feasible(design)
        except ImpossibleExperimentDesignError as exc:
            return _rejected(design, floor_days, reason=str(exc))
        duration_days = max(floor_days, round(design.weeks_needed * 7))
        return ExperimentDesignView(
            feasible=True,
            sample_per_arm=design.sample_per_arm,
            weeks_needed=design.weeks_needed,
            duration_days=duration_days,
            minimum_duration_days=floor_days,
            rejection_reason=None,
        )


def _rejected(design: SampleSizeDesign, floor_days: int, *, reason: str) -> ExperimentDesignView:
    return ExperimentDesignView(
        feasible=False,
        sample_per_arm=design.sample_per_arm,
        weeks_needed=design.weeks_needed,
        duration_days=0,
        minimum_duration_days=floor_days,
        rejection_reason=reason,
    )


__all__ = ["DesignExperiment", "DesignExperimentRequest"]
