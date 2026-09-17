"""Forma comun de `IngestionCycle`/`SignalCycle`: recorrer los negocios
activos y correr el mismo paso, con nombre de paso propio, en cada uno
(plan.md §7). `NotificationCycle` no encaja aqui: publica, no "ingiere",
y su enrutado digest/ticker es su propia logica (`notification_cycle.py`)."""

from __future__ import annotations

from safent_ads.orchestration.application.cycle_step_runner import (
    StepOperation,
    run_step_for_business,
)
from safent_ads.orchestration.application.logging import log_cycle_outcome, log_cycle_start
from safent_ads.orchestration.application.ports import BusinessListingPort
from safent_ads.orchestration.domain.models import CycleReport
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator


async def run_per_business_cycle(
    *,
    cycle_name: str,
    step_name: str,
    operation: StepOperation,
    businesses: BusinessListingPort,
    clock: Clock,
    id_generator: IdGenerator,
    cycle_id: str | None,
) -> CycleReport:
    resolved_cycle_id = cycle_id or str(id_generator.new_id())
    started_at = clock.now()
    log_cycle_start(cycle_name, resolved_cycle_id)

    business_ids = await businesses.list_active_business_ids()
    results = [
        await run_step_for_business(
            step_name,
            operation,
            business_id=business_id,
            cycle_id=resolved_cycle_id,
            now=started_at,
        )
        for business_id in business_ids
    ]

    finished_at = clock.now()
    report = CycleReport(cycle_name, resolved_cycle_id, started_at, finished_at, tuple(results))
    log_cycle_outcome(report)
    return report
