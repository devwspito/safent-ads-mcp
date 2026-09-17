"""`RuleCalibrationCycle` (profitability-engine.md §6, tasks.md T200):
recalibra los umbrales `AUTO` a partir de los `SignalOutcome` acumulados.
Global, no por negocio (`rules` es catalogo `scope = 'global'`, ver
Assumption de `RecalibrateRules`): un unico paso, no
`run_per_business_cycle`. `calibration_adjustments` (UNIQUE por semana)
hace que llamarlo en cada vuelta del worker sea barato y correcto: como
mucho un ajuste real por regla y semana, el resto de vueltas es un chequeo
corto que no ajusta nada."""

from __future__ import annotations

from datetime import datetime

from safent_ads.orchestration.application.logging import log_cycle_outcome, log_cycle_start
from safent_ads.orchestration.application.ports import RuleCalibrationStepPort
from safent_ads.orchestration.application.retry import run_with_retry
from safent_ads.orchestration.domain.models import CycleReport, StepOutcome, StepResult
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

_CYCLE_NAME = "rule_calibration"
_STEP_NAME = "recalibrate_rules"


class RuleCalibrationCycle:
    def __init__(
        self,
        *,
        calibration_step: RuleCalibrationStepPort,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._calibration_step = calibration_step
        self._clock = clock
        self._id_generator = id_generator

    async def execute(self, *, cycle_id: str | None = None) -> CycleReport:
        resolved_cycle_id = cycle_id or str(self._id_generator.new_id())
        started_at = self._clock.now()
        log_cycle_start(_CYCLE_NAME, resolved_cycle_id)
        result = await self._run_step(resolved_cycle_id, started_at)
        finished_at = self._clock.now()
        report = CycleReport(_CYCLE_NAME, resolved_cycle_id, started_at, finished_at, (result,))
        log_cycle_outcome(report)
        return report

    async def _run_step(self, cycle_id: str, started_at: datetime) -> StepResult:
        try:
            attempts = await run_with_retry(
                lambda: self._calibration_step.run(cycle_id, started_at)
            )
        except Exception as exc:  # noqa: BLE001 - limite del ciclo: se registra y se continua
            return StepResult(_STEP_NAME, None, StepOutcome.FAILED, attempts=3, error=str(exc))
        return StepResult(_STEP_NAME, None, StepOutcome.SUCCESS, attempts=attempts)
