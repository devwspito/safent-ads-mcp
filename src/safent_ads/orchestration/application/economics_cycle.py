"""`EconomicsCycle` (T156, plan.md §7): perfil de economia unitaria, curva
de rezago y divergencia de plataforma, por negocio. El paso real vive en
`orchestration.infrastructure.economics_step.LiveEconomicsStep`; aqui solo
se orquesta (mismo patron que `IngestionCycle`/`SignalCycle`)."""

from __future__ import annotations

from safent_ads.orchestration.application.per_business_cycle import run_per_business_cycle
from safent_ads.orchestration.application.ports import BusinessListingPort, EconomicsStepPort
from safent_ads.orchestration.domain.models import CycleReport
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

_CYCLE_NAME = "economics"
_STEP_NAME = "fill_unit_economics"


class EconomicsCycle:
    def __init__(
        self,
        *,
        businesses: BusinessListingPort,
        economics_step: EconomicsStepPort,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._businesses = businesses
        self._economics_step = economics_step
        self._clock = clock
        self._id_generator = id_generator

    async def execute(self, *, cycle_id: str | None = None) -> CycleReport:
        return await run_per_business_cycle(
            cycle_name=_CYCLE_NAME,
            step_name=_STEP_NAME,
            operation=self._economics_step.run,
            businesses=self._businesses,
            clock=self._clock,
            id_generator=self._id_generator,
            cycle_id=cycle_id,
        )
