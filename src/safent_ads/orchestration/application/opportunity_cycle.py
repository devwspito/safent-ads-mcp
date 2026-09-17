"""`OpportunityCycle` (tasks.md T113, FR-35): por negocio, genera
`OpportunityCandidate`s desde huecos de calendario sin cobertura y los
materializa como `Proposal`s `CREATE_CAMPAIGN`. El paso real
(`GenerateOpportunities`) vive en `opportunities` (otra lane logica dentro
de este mismo repo); aqui solo se orquesta -- mismo patron que
`SignalOutcomeCycle`. Corre en cada vuelta del worker (15/60 min, igual que
`RuleCalibrationCycle`): idempotente por `FR-20` (una sola propuesta
abierta por candidato) y por el cupo diario ya contado, asi que repetirlo
dentro del mismo dia no duplica ni sobrepasa NFR-11."""

from __future__ import annotations

from safent_ads.orchestration.application.per_business_cycle import run_per_business_cycle
from safent_ads.orchestration.application.ports import BusinessListingPort, OpportunityStepPort
from safent_ads.orchestration.domain.models import CycleReport
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

_CYCLE_NAME = "opportunities"
_STEP_NAME = "generate_opportunities"


class OpportunityCycle:
    def __init__(
        self,
        *,
        businesses: BusinessListingPort,
        opportunity_step: OpportunityStepPort,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._businesses = businesses
        self._opportunity_step = opportunity_step
        self._clock = clock
        self._id_generator = id_generator

    async def execute(self, *, cycle_id: str | None = None) -> CycleReport:
        return await run_per_business_cycle(
            cycle_name=_CYCLE_NAME,
            step_name=_STEP_NAME,
            operation=self._opportunity_step.run,
            businesses=self._businesses,
            clock=self._clock,
            id_generator=self._id_generator,
            cycle_id=cycle_id,
        )
