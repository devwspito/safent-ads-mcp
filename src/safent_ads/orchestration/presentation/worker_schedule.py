"""Bucle del `ads-worker` (plan.md §7, T047): `IngestionCycle`/`EconomicsCycle`/
`SignalCycle` cada 15 min en horario activo / 60 min fuera de el;
`NotificationCycle` segun el ticker programado (misma cadencia que
`IngestionCycle`: cada vuelta decide sola si toca enviar o acumular,
contracts/telegram.md). `EconomicsCycle` (T156) va justo despues de la
ingesta y antes de `SignalCycle`: llena `unit_economics_profiles`/
`lag_curve_snapshots`/`platform_divergence_snapshots` con metricas y CRM ya
frescos, para que la puerta de rezago (T157) pueda leer una curva
actualizada en la misma vuelta. `croniter` no hace falta aqui: son
intervalos fijos conocidos en diseno (pyproject.toml ya lo justifica para
las cadencias tipo cron; estas no lo son)."""

from __future__ import annotations

import asyncio

import structlog

from safent_ads.notifications.domain.value_objects import ActiveHoursWindow
from safent_ads.orchestration.infrastructure.runtime import OrchestrationRuntime
from safent_ads.shared.clock import Clock

logger = structlog.get_logger(__name__)

_ACTIVE_INTERVAL_SECONDS = 15 * 60
_INACTIVE_INTERVAL_SECONDS = 60 * 60
# plan.md §7: `ExecutionCycle` corre cada 30 s, independiente del resto --
# un intento agendado con gracia de segundos (`RuleCycle`,
# `AuthorizeRuleAction`) no puede esperar hasta el siguiente tick de 15 min.
_EXECUTION_INTERVAL_SECONDS = 30


def _next_interval_seconds(active_hours: ActiveHoursWindow, clock: Clock) -> int:
    if active_hours.is_active(clock.now()):
        return _ACTIVE_INTERVAL_SECONDS
    return _INACTIVE_INTERVAL_SECONDS


async def run_forever(
    runtime: OrchestrationRuntime,
    *,
    active_hours: ActiveHoursWindow,
    clock: Clock,
    stop_event: asyncio.Event,
) -> None:
    """Cada vuelta: ingesta, senales, reglas, notificaciones, en ese orden
    (una senal necesita metricas frescas; una regla necesita la ultima
    senal; una notificacion necesita senales evaluadas). Un ciclo con
    fallos parciales no detiene el bucle — el siguiente tick reintenta
    desde cero, idempotente por diseno. `ExecutionCycle` (30 s) corre en su
    propio bucle -- ver `run_execution_forever`, arrancado por separado en
    `composition/worker.py`."""
    while not stop_event.is_set():
        await runtime.ingestion_cycle.execute()
        await runtime.economics_cycle.execute()
        await runtime.signal_cycle.execute()
        await runtime.rule_cycle.execute()
        await runtime.notification_cycle.execute()
        # Contraste a 14 dias (profitability-engine.md §6): idempotente por
        # `signal_outcomes` (UNIQUE en signal_id), barato correrlo cada
        # vuelta -- la consulta ya filtra por vencidas sin resolver.
        await runtime.signal_outcome_cycle.execute()
        # Calibracion semanal (profitability-engine.md §6): idempotente por
        # `calibration_adjustments` (UNIQUE por regla+umbral+semana ISO) --
        # como mucho un ajuste real por semana, el resto de vueltas es un
        # chequeo corto.
        await runtime.rule_calibration_cycle.execute()
        # Oportunidades semanales (tasks.md T113, FR-35): idempotente por
        # FR-20 (una sola propuesta abierta por candidato) y por el cupo
        # diario ya contado (NFR-11) -- correrlo cada vuelta no duplica ni
        # sobrepasa el presupuesto de atencion.
        await runtime.opportunity_cycle.execute()
        # Mantenimiento diario (plan.md §7, tasks.md T078): expirar
        # propuestas, purgar Telegram, verificar la cadena y reconciliar
        # CRM -- cada sub-paso es idempotente por su propia `WHERE`.
        await runtime.maintenance_cycle.execute()
        # Salud de credenciales (tasks.md T126, threat-model.md C-21): el
        # chequeo es una lectura local del broker, nunca contacta Google/
        # Meta -- correrlo cada vuelta detecta a tiempo un cruce del
        # umbral de `expiring_soon` (FR/NFR de frescura de esta alerta).
        await runtime.credential_health_cycle.execute()

        interval = _next_interval_seconds(active_hours, clock)
        logger.info("ads_worker_sleeping", seconds=interval)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue


async def run_execution_forever(
    runtime: OrchestrationRuntime, *, stop_event: asyncio.Event
) -> None:
    """`ExecutionCycle` a cadencia fija de 30 s (plan.md §7), en un bucle
    propio: no comparte el intervalo 15/60 min de `run_forever` porque la
    gracia servidora que agenda una ejecucion se mide en segundos, no en
    minutos."""
    while not stop_event.is_set():
        await runtime.execution_cycle.execute()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_EXECUTION_INTERVAL_SECONDS)
        except TimeoutError:
            continue
