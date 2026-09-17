"""`SignalCycle` (plan.md §7, T047): puertas -> senales -> anomalias ->
pacing por negocio. El paso real (`EvaluateEntitySignals`/`DetectAnomalies`)
vive en `signals` (otra lane); aqui solo se orquesta."""

from __future__ import annotations

from safent_ads.orchestration.application.per_business_cycle import run_per_business_cycle
from safent_ads.orchestration.application.ports import (
    BusinessListingPort,
    SignalEvaluationStepPort,
)
from safent_ads.orchestration.domain.models import CycleReport
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

_CYCLE_NAME = "signals"
_STEP_NAME = "evaluate_signals"


class SignalCycle:
    def __init__(
        self,
        *,
        businesses: BusinessListingPort,
        signal_step: SignalEvaluationStepPort,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._businesses = businesses
        self._signal_step = signal_step
        self._clock = clock
        self._id_generator = id_generator

    async def execute(self, *, cycle_id: str | None = None) -> CycleReport:
        return await run_per_business_cycle(
            cycle_name=_CYCLE_NAME,
            step_name=_STEP_NAME,
            operation=self._signal_step.run,
            businesses=self._businesses,
            clock=self._clock,
            id_generator=self._id_generator,
            cycle_id=cycle_id,
        )
