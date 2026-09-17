"""`CredentialHealthCycle` (tasks.md T126, threat-model.md C-21): por
negocio, comprueba la salud de las credenciales de plataforma conectadas.
El paso real (`CheckCredentialHealth` + alerta + `decision_log`) vive en
`accounts`/`notifications`/`audit` (otras lanes); aqui solo se orquesta,
igual que `IngestionCycle`.

Corre en cada vuelta del worker (15/60 min, `orchestration/presentation/
worker_schedule.py`), sin cadencia propia de horas: el chequeo es una
lectura local del broker (nunca contacta Google/Meta, `connect_ports.py`),
barata de repetir, y una cadencia mas espaciada dejaria sin detectar a
tiempo una credencial que cruza el umbral de `expiring_soon` entre
vueltas -- C-21 exige que la alerta llegue a tiempo, no una cadencia
propia."""

from __future__ import annotations

from safent_ads.orchestration.application.per_business_cycle import run_per_business_cycle
from safent_ads.orchestration.application.ports import BusinessListingPort, CredentialHealthStepPort
from safent_ads.orchestration.domain.models import CycleReport
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

_CYCLE_NAME = "credential_health"
_STEP_NAME = "check_credential_health"


class CredentialHealthCycle:
    def __init__(
        self,
        *,
        businesses: BusinessListingPort,
        credential_health_step: CredentialHealthStepPort,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._businesses = businesses
        self._credential_health_step = credential_health_step
        self._clock = clock
        self._id_generator = id_generator

    async def execute(self, *, cycle_id: str | None = None) -> CycleReport:
        return await run_per_business_cycle(
            cycle_name=_CYCLE_NAME,
            step_name=_STEP_NAME,
            operation=self._credential_health_step.run,
            businesses=self._businesses,
            clock=self._clock,
            id_generator=self._id_generator,
            cycle_id=cycle_id,
        )
