"""`ExecutionCycle` (plan.md §7: cadencia 30 s; tasks.md T068): drena la
cola de `executions` reclamando y ejecutando un intento a la vez hasta que
no quede nada listo. Cada intento es su propia transaccion/sesion --
`run_once` (via `ExecutionChokepointRunner`) confirma cuando termina; un
intento que falla nunca bloquea a los demas de la vuelta (default-deny,
plan.md §6)."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol

import structlog

from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.observability.metrics import record_cycle, record_execution_outcome
from safent_ads.orchestration.domain.models import CycleReport, StepOutcome, StepResult
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

logger = structlog.get_logger(__name__)

_OUTCOME_OK = "ok"
_OUTCOME_PARTIAL_FAILURE = "partial_failure"

_CYCLE_NAME = "execution"
_STEP_NAME = "claim_and_run"
# Tope de intentos por vuelta: si la cola nunca se vacia (p. ej. un bróker
# caído que rechaza todo, o un error en cadena), la vuelta termina igual --
# el siguiente tick del bucle de 30 s retoma, en vez de que un ciclo se
# quede corriendo indefinidamente.
_MAX_ATTEMPTS_PER_CYCLE = 200


class ExecutionChokepointRunner(Protocol):
    """Una llamada = una sesion/transaccion completa: reclama, procesa y
    confirma un `ExecutionAttempt`, o devuelve `None` si no habia nada que
    reclamar. `Container.build_execution_use_cases(session).chokepoint.
    run_once()` mas el `commit()` de la sesion es la implementacion real
    (`composition/worker.py`)."""

    def __call__(self) -> Awaitable[ExecutionStatus | None]: ...


class ExecutionCycle:
    def __init__(
        self,
        *,
        run_once: ExecutionChokepointRunner,
        clock: Clock,
        id_generator: IdGenerator,
        max_attempts_per_cycle: int = _MAX_ATTEMPTS_PER_CYCLE,
    ) -> None:
        self._run_once = run_once
        self._clock = clock
        self._id_generator = id_generator
        self._max_attempts_per_cycle = max_attempts_per_cycle

    async def execute(self, *, cycle_id: str | None = None) -> CycleReport:
        resolved_cycle_id = cycle_id or str(self._id_generator.new_id())
        started_at = self._clock.now()
        logger.info("orchestration_cycle_start", cycle=_CYCLE_NAME, cycle_id=resolved_cycle_id)

        results: list[StepResult] = []
        for _ in range(self._max_attempts_per_cycle):
            result = await self._claim_and_run(resolved_cycle_id)
            if result is None:
                break
            results.append(result)

        finished_at = self._clock.now()
        report = CycleReport(
            _CYCLE_NAME, resolved_cycle_id, started_at, finished_at, tuple(results)
        )
        ok_event = "orchestration_cycle_ok"
        partial_event = "orchestration_cycle_partial_failure"
        event = ok_event if report.all_succeeded else partial_event
        logger.info(
            event,
            cycle=_CYCLE_NAME,
            cycle_id=resolved_cycle_id,
            claimed=len(results),
            failed=len(report.failed_results),
        )
        record_cycle(
            _CYCLE_NAME,
            outcome=_OUTCOME_OK if report.all_succeeded else _OUTCOME_PARTIAL_FAILURE,
            duration_seconds=(finished_at - started_at).total_seconds(),
        )
        return report

    async def _claim_and_run(self, cycle_id: str) -> StepResult | None:
        log = logger.bind(cycle_id=cycle_id, step=_STEP_NAME)
        try:
            outcome = await self._run_once()
        except Exception as exc:  # noqa: BLE001 - default-deny: un fallo tecnico no para el ciclo
            log.error("orchestration_step_failed", error=str(exc))
            return StepResult(_STEP_NAME, None, StepOutcome.FAILED, attempts=1, error=str(exc))
        if outcome is None:
            return None
        # Todo desenlace que el chokepoint devuelve sin lanzar es un exito
        # DEL CICLO (reclamo -> desenlace o UNKNOWN persistido) aunque el
        # desenlace de NEGOCIO sea un bloqueo -- `BLOCKED_GUARDRAIL`/
        # `BLOCKED_BRAKE` son el sistema funcionando como debe, no un fallo
        # tecnico. Solo una excepcion sin capturar (el `except` de arriba)
        # cuenta como `StepOutcome.FAILED` para este informe.
        log.info("execution_cycle_claimed", outcome=outcome.value)
        record_execution_outcome(outcome.value)
        return StepResult(_STEP_NAME, None, StepOutcome.SUCCESS, attempts=1)
