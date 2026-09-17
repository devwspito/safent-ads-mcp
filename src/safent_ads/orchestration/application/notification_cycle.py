"""`NotificationCycle` (plan.md §7, T047): vacia el digest pendiente si toca
y publica el ticker de cada negocio (contracts/telegram.md). Las senales
`CRITICAL` no pasan por aqui: interrumpen siempre, fuera del ciclo
programado (contracts/telegram.md §Digest: "Las senales CRITICAL no se
acumulan")."""

from __future__ import annotations

from datetime import datetime

from safent_ads.notifications.application.publish_digest import PublishDigest
from safent_ads.notifications.application.publish_ticker import PublishTicker
from safent_ads.orchestration.application.cycle_step_runner import (
    StepOperation,
    run_step_for_business,
)
from safent_ads.orchestration.application.logging import log_cycle_outcome, log_cycle_start
from safent_ads.orchestration.application.ports import NotificationTarget, NotificationTargetsPort
from safent_ads.orchestration.domain.models import CycleReport, StepResult
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, IdGenerator

_CYCLE_NAME = "notification"
_DIGEST_STEP = "publish_digest"
_TICKER_STEP = "publish_ticker"


class NotificationCycle:
    def __init__(
        self,
        *,
        targets: NotificationTargetsPort,
        publish_digest: PublishDigest,
        publish_ticker: PublishTicker,
        clock: Clock,
        id_generator: IdGenerator,
    ) -> None:
        self._targets = targets
        self._publish_digest = publish_digest
        self._publish_ticker = publish_ticker
        self._clock = clock
        self._id_generator = id_generator

    async def execute(self, *, cycle_id: str | None = None) -> CycleReport:
        resolved_cycle_id = cycle_id or str(self._id_generator.new_id())
        started_at = self._clock.now()
        log_cycle_start(_CYCLE_NAME, resolved_cycle_id)

        targets = await self._targets.list_notification_targets()
        results: list[StepResult] = []
        for target in targets:
            results.append(
                await run_step_for_business(
                    _DIGEST_STEP,
                    self._digest_operation(target),
                    business_id=target.business_id,
                    cycle_id=resolved_cycle_id,
                    now=started_at,
                )
            )
            results.append(
                await run_step_for_business(
                    _TICKER_STEP,
                    self._ticker_operation(target),
                    business_id=target.business_id,
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

    def _digest_operation(self, target: NotificationTarget) -> StepOperation:
        async def operation(_business_id: BusinessId, _cycle_id: str, now: datetime) -> None:
            await self._publish_digest.execute(
                business_id=target.business_id,
                business_name=target.business_name,
                owner_chat_ids=list(target.owner_chat_ids),
                now=now,
            )

        return operation

    def _ticker_operation(self, target: NotificationTarget) -> StepOperation:
        async def operation(_business_id: BusinessId, _cycle_id: str, now: datetime) -> None:
            await self._publish_ticker.execute(
                business_id=target.business_id,
                business_name=target.business_name,
                owner_chat_ids=list(target.owner_chat_ids),
                now=now,
            )

        return operation
