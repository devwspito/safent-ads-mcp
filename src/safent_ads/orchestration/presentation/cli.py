"""CLI (T047): `python -m safent_ads.orchestration --cycle X --once`.
Solo soporta `--once`: el bucle continuo (horario/15 min segun horario
activo, diario estructural) lo gestiona `ads-worker`
(`presentation/worker_schedule.py`), no la CLI."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Coroutine

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.infrastructure.oauth_broker_client import OAuthBrokerSocketClient
from safent_ads.composition.container import Container, ExecutionUseCases
from safent_ads.composition.settings import WorkerSettings
from safent_ads.logging_setup import configure_logging
from safent_ads.notifications.domain.value_objects import ActiveHoursWindow
from safent_ads.orchestration.domain.models import CycleReport
from safent_ads.orchestration.infrastructure.runtime import (
    OrchestrationRuntime,
    build_default_runtime,
)

logger = structlog.get_logger(__name__)

# `quickstart.md §6.4`/`§7`/`§8`/`§9`: la prueba de humo de F2/F3/F5(US5)/F9
# pide `--cycle rules --once`, `--cycle execution --once`, `--cycle
# opportunities --once`, `--cycle maintenance --once`, `--cycle
# signal_outcomes --once` y `--cycle rule_calibration --once` explicitamente.
_CYCLE_CHOICES = (
    "ingest",
    "signals",
    "rules",
    "execution",
    "notify",
    "opportunities",
    "maintenance",
    "signal_outcomes",
    "rule_calibration",
    "credential_health",
)


def _cycle_runner(
    runtime: OrchestrationRuntime, cycle_name: str
) -> Callable[[], Coroutine[object, object, CycleReport]]:
    runners: dict[str, Callable[[], Coroutine[object, object, CycleReport]]] = {
        "ingest": runtime.ingestion_cycle.execute,
        "signals": runtime.signal_cycle.execute,
        "rules": runtime.rule_cycle.execute,
        "execution": runtime.execution_cycle.execute,
        "notify": runtime.notification_cycle.execute,
        "opportunities": runtime.opportunity_cycle.execute,
        "maintenance": runtime.maintenance_cycle.execute,
        "signal_outcomes": runtime.signal_outcome_cycle.execute,
        "rule_calibration": runtime.rule_calibration_cycle.execute,
        "credential_health": runtime.credential_health_cycle.execute,
    }
    return runners[cycle_name]


def _execution_use_cases_factory(
    container: Container,
) -> Callable[[AsyncSession], ExecutionUseCases]:
    def factory(session: AsyncSession) -> ExecutionUseCases:
        return container.build_execution_use_cases(session)

    return factory


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m safent_ads.orchestration")
    parser.add_argument("--cycle", choices=_CYCLE_CHOICES, required=True)
    parser.add_argument("--once", action="store_true", required=True)
    return parser.parse_args(argv)


async def _run_once(cycle_name: str) -> CycleReport:
    settings = WorkerSettings()  # type: ignore[call-arg]
    container = Container.build(settings)
    try:
        runtime = build_default_runtime(
            session_factory=container.session_factory,
            platform_port=container.ads_platform_port,
            oauth_broker=OAuthBrokerSocketClient(settings.broker_socket_path),
            active_hours=ActiveHoursWindow.parse(
                settings.active_hours, tz_name=settings.timezone
            ),
            execution_use_cases_factory=_execution_use_cases_factory(container),
            telegram_bot_token=settings.telegram_bot_token.get_secret_value() or None,
            telegram_owner_chat_ids=tuple(settings.telegram_owner_chat_ids),
            digest_hour_default=settings.digest_hour_default,
        )
        return await _cycle_runner(runtime, cycle_name)()
    finally:
        await container.aclose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging()

    report = asyncio.run(_run_once(args.cycle))
    if not report.all_succeeded:
        logger.error(
            "orchestration_cli_partial_failure",
            cycle=args.cycle,
            failed=[r.business_id for r in report.failed_results],
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
