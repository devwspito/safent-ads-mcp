"""Traza uniforme de arranque/fin de ciclo (plan.md §7: "cada ciclo ...
escribe traza"), compartida por los tres ciclos."""

from __future__ import annotations

import structlog

from safent_ads.observability.metrics import record_cycle
from safent_ads.orchestration.domain.models import CycleReport

logger = structlog.get_logger(__name__)

_OUTCOME_OK = "ok"
_OUTCOME_PARTIAL_FAILURE = "partial_failure"


def log_cycle_start(cycle_name: str, cycle_id: str) -> None:
    logger.info("orchestration_cycle_start", cycle=cycle_name, cycle_id=cycle_id)


def log_cycle_outcome(report: CycleReport) -> None:
    outcome = _OUTCOME_OK if report.all_succeeded else _OUTCOME_PARTIAL_FAILURE
    event = (
        "orchestration_cycle_ok" if report.all_succeeded else "orchestration_cycle_partial_failure"
    )
    logger.info(
        event,
        cycle=report.cycle_name,
        cycle_id=report.cycle_id,
        failed=len(report.failed_results),
        total=len(report.results),
    )
    record_cycle(
        report.cycle_name,
        outcome=outcome,
        duration_seconds=(report.finished_at - report.started_at).total_seconds(),
    )
