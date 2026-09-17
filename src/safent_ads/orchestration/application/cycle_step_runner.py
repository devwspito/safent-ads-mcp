"""Ejecuta un paso por negocio con traza y reintento, y produce su
`StepResult` sin lanzar (el fallo de un negocio no debe abortar el ciclo
para los demas — plan.md §7)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog

from safent_ads.observability.metrics import record_step
from safent_ads.orchestration.application.retry import DEFAULT_MAX_ATTEMPTS, run_with_retry
from safent_ads.orchestration.domain.models import StepOutcome, StepResult
from safent_ads.shared.ids import BusinessId

logger = structlog.get_logger(__name__)

StepOperation = Callable[[BusinessId, str, datetime], Awaitable[None]]


async def run_step_for_business(
    step_name: str,
    operation: StepOperation,
    *,
    business_id: BusinessId,
    cycle_id: str,
    now: datetime,
) -> StepResult:
    log = logger.bind(cycle_id=cycle_id, step=step_name, business_id=str(business_id))
    log.info("orchestration_step_start")
    try:
        attempts = await run_with_retry(lambda: operation(business_id, cycle_id, now))
    except Exception as exc:  # noqa: BLE001 - limite del ciclo: se registra y se continua
        log.error("orchestration_step_failed", error=str(exc))
        record_step(step_name, outcome=StepOutcome.FAILED.value)
        return StepResult(
            step_name,
            str(business_id),
            StepOutcome.FAILED,
            attempts=DEFAULT_MAX_ATTEMPTS,
            error=str(exc),
        )
    log.info("orchestration_step_ok", attempts=attempts)
    record_step(step_name, outcome=StepOutcome.SUCCESS.value)
    return StepResult(step_name, str(business_id), StepOutcome.SUCCESS, attempts=attempts)
