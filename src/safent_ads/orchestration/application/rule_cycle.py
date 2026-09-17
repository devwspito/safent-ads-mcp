"""`RuleCycle` (plan.md §7, tasks.md T068): senal -> regla -> autorizacion
de regla `AUTO` o propuesta `pendiente`. El paso real (leer la ultima
senal, decidir por `evaluate_rule`, crear/autorizar la propuesta) vive en
`orchestration.infrastructure.live_steps.LiveRuleStep`; aqui solo se
orquesta: recorrer negocios, reintentar, trazar (mismo patron que
`IngestionCycle`/`SignalCycle`)."""

from __future__ import annotations

from safent_ads.orchestration.application.per_business_cycle import run_per_business_cycle
from safent_ads.orchestration.application.ports import BusinessListingPort, RuleEvaluationStepPort
from safent_ads.orchestration.domain.models import CycleReport
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

_CYCLE_NAME = "rules"
_STEP_NAME = "evaluate_rules"


class RuleCycle:
    def __init__(
        self,
        *,
        businesses: BusinessListingPort,
        rule_step: RuleEvaluationStepPort,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._businesses = businesses
        self._rule_step = rule_step
        self._clock = clock
        self._id_generator = id_generator

    async def execute(self, *, cycle_id: str | None = None) -> CycleReport:
        return await run_per_business_cycle(
            cycle_name=_CYCLE_NAME,
            step_name=_STEP_NAME,
            operation=self._rule_step.run,
            businesses=self._businesses,
            clock=self._clock,
            id_generator=self._id_generator,
            cycle_id=cycle_id,
        )
