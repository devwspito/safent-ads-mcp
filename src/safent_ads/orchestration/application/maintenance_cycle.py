"""`MaintenanceCycle` (plan.md §7, tasks.md T078): limpieza diaria que
ningun otro ciclo hace -- caducar propuestas vencidas, purgar nonces y
codigos de emparejamiento de Telegram consumidos/caducados, verificar la
cadena del `decision_log` (globales, sin negocio propio) y reconciliar
plataforma vs CRM por negocio (T116). Mismo patron que `NotificationCycle`:
pasos de distinta naturaleza dentro de un unico `CycleReport`, no el
helper `run_per_business_cycle` de un solo paso.

Corre en cada vuelta del worker (15/60 min, igual que `RuleCalibrationCycle`):
cada sub-paso es idempotente por su propia `WHERE` (expirar solo lo vencido,
purgar solo lo caducado, reconciliar solo senales sin `contradicted_at`),
asi que repetirlo dentro del mismo dia no duplica nada."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from safent_ads.orchestration.application.cycle_step_runner import run_step_for_business
from safent_ads.orchestration.application.logging import log_cycle_outcome, log_cycle_start
from safent_ads.orchestration.application.ports import BusinessListingPort, MaintenanceStepPort
from safent_ads.orchestration.application.retry import DEFAULT_MAX_ATTEMPTS, run_with_retry
from safent_ads.orchestration.domain.models import CycleReport, StepOutcome, StepResult
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

_CYCLE_NAME = "maintenance"
_EXPIRE_PROPOSALS_STEP = "expire_stale_proposals"
_PURGE_TELEGRAM_STEP = "purge_telegram_artifacts"
_VERIFY_CHAIN_STEP = "verify_decision_log_chain"
_RECONCILE_STEP = "reconcile_platform_vs_crm"


class MaintenanceCycle:
    def __init__(
        self,
        *,
        businesses: BusinessListingPort,
        maintenance_step: MaintenanceStepPort,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._businesses = businesses
        self._maintenance_step = maintenance_step
        self._clock = clock
        self._id_generator = id_generator

    async def execute(self, *, cycle_id: str | None = None) -> CycleReport:
        resolved_cycle_id = cycle_id or str(self._id_generator.new_id())
        started_at = self._clock.now()
        log_cycle_start(_CYCLE_NAME, resolved_cycle_id)

        results = [
            await self._run_global_step(
                _EXPIRE_PROPOSALS_STEP, self._maintenance_step.expire_stale_proposals, started_at
            ),
            await self._run_global_step(
                _PURGE_TELEGRAM_STEP, self._maintenance_step.purge_telegram_artifacts, started_at
            ),
            await self._run_global_step(
                _VERIFY_CHAIN_STEP, self._maintenance_step.verify_decision_log_chain, started_at
            ),
        ]
        business_ids = await self._businesses.list_active_business_ids()
        for business_id in business_ids:
            results.append(
                await run_step_for_business(
                    _RECONCILE_STEP,
                    self._maintenance_step.reconcile_platform_vs_crm,
                    business_id=business_id,
                    cycle_id=resolved_cycle_id,
                    now=started_at,
                )
            )

        finished_at = self._clock.now()
        report = CycleReport(
            _CYCLE_NAME, resolved_cycle_id, started_at, finished_at, tuple(results)
        )
        log_cycle_outcome(report)
        return report

    async def _run_global_step(
        self, step_name: str, operation: Callable[[datetime], Awaitable[None]], now: datetime
    ) -> StepResult:
        try:
            attempts = await run_with_retry(lambda: operation(now))
        except Exception as exc:  # noqa: BLE001 - limite del ciclo: se registra y se continua
            return StepResult(
                step_name, None, StepOutcome.FAILED, attempts=DEFAULT_MAX_ATTEMPTS, error=str(exc)
            )
        return StepResult(step_name, None, StepOutcome.SUCCESS, attempts=attempts)
