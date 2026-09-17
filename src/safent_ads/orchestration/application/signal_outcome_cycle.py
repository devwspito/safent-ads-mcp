"""`SignalOutcomeCycle` (profitability-engine.md §6, tasks.md T199):
resuelve el contraste a 14 dias de las senales accionables vencidas de cada
negocio. El paso real (`EvaluateSignalOutcomes`) vive en `optimization`
(otra lane); aqui solo se orquesta -- mismo patron que `SignalCycle`."""

from __future__ import annotations

from safent_ads.orchestration.application.per_business_cycle import run_per_business_cycle
from safent_ads.orchestration.application.ports import (
    BusinessListingPort,
    SignalOutcomeEvaluationStepPort,
)
from safent_ads.orchestration.domain.models import CycleReport
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

_CYCLE_NAME = "signal_outcomes"
_STEP_NAME = "evaluate_signal_outcomes"


class SignalOutcomeCycle:
    def __init__(
        self,
        *,
        businesses: BusinessListingPort,
        signal_outcome_step: SignalOutcomeEvaluationStepPort,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._businesses = businesses
        self._signal_outcome_step = signal_outcome_step
        self._clock = clock
        self._id_generator = id_generator

    async def execute(self, *, cycle_id: str | None = None) -> CycleReport:
        return await run_per_business_cycle(
            cycle_name=_CYCLE_NAME,
            step_name=_STEP_NAME,
            operation=self._signal_outcome_step.run,
            businesses=self._businesses,
            clock=self._clock,
            id_generator=self._id_generator,
            cycle_id=cycle_id,
        )
