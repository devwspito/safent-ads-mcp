"""Cablea los ciclos de F1/F2/F9/F10/F12 (`IngestionCycle`/`SignalCycle`/
`RuleCycle`/`ExecutionCycle`/`NotificationCycle`/`SignalOutcomeCycle`/
`RuleCalibrationCycle`/`EconomicsCycle`/`OpportunityCycle`/
`MaintenanceCycle`/`CredentialHealthCycle`) con los adaptadores reales
(`live_steps.py`, `rule_step.py`,
`notifications/infrastructure/sql_repositories.py`) -- la forma
(`OrchestrationRuntime`) no cambia, solo lo que hay detras de cada
puerto."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.application.connect_ports import OAuthBrokerPort
from safent_ads.accounts.application.ports import AdsPlatformPort
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.notifications.application.dto import TickerSignal
from safent_ads.notifications.application.ports import MessengerPort
from safent_ads.notifications.application.publish_credential_health_alert import (
    PublishCredentialHealthAlert,
)
from safent_ads.notifications.application.publish_digest import PublishDigest
from safent_ads.notifications.application.publish_ticker import PublishTicker
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.value_objects import ActiveHoursWindow
from safent_ads.notifications.infrastructure.null_messenger import NullMessenger
from safent_ads.notifications.infrastructure.sql_repositories import (
    SqlNotificationOutbox,
    SqlPendingDigest,
    SqlSignalsForTicker,
)
from safent_ads.orchestration.application.credential_health_cycle import CredentialHealthCycle
from safent_ads.orchestration.application.economics_cycle import EconomicsCycle
from safent_ads.orchestration.application.execution_cycle import ExecutionCycle
from safent_ads.orchestration.application.ingestion_cycle import IngestionCycle
from safent_ads.orchestration.application.maintenance_cycle import MaintenanceCycle
from safent_ads.orchestration.application.notification_cycle import NotificationCycle
from safent_ads.orchestration.application.opportunity_cycle import OpportunityCycle
from safent_ads.orchestration.application.rule_calibration_cycle import RuleCalibrationCycle
from safent_ads.orchestration.application.rule_cycle import RuleCycle
from safent_ads.orchestration.application.signal_cycle import SignalCycle
from safent_ads.orchestration.application.signal_outcome_cycle import SignalOutcomeCycle
from safent_ads.orchestration.infrastructure.credential_health_step import LiveCredentialHealthStep
from safent_ads.orchestration.infrastructure.economics_step import LiveEconomicsStep
from safent_ads.orchestration.infrastructure.live_steps import (
    LiveIngestionStep,
    LiveSignalStep,
    SqlBusinessListing,
    SqlNotificationTargets,
)
from safent_ads.orchestration.infrastructure.maintenance_step import LiveMaintenanceStep
from safent_ads.orchestration.infrastructure.opportunity_step import LiveOpportunityStep
from safent_ads.orchestration.infrastructure.rule_calibration_step import LiveRuleCalibrationStep
from safent_ads.orchestration.infrastructure.rule_step import ExecutionUseCasesFactory, LiveRuleStep
from safent_ads.orchestration.infrastructure.signal_outcome_step import LiveSignalOutcomeStep
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.ids import BusinessId, IdGenerator, UuidIdGenerator


@dataclass(frozen=True, slots=True)
class OrchestrationRuntime:
    ingestion_cycle: IngestionCycle
    signal_cycle: SignalCycle
    rule_cycle: RuleCycle
    execution_cycle: ExecutionCycle
    notification_cycle: NotificationCycle
    signal_outcome_cycle: SignalOutcomeCycle
    rule_calibration_cycle: RuleCalibrationCycle
    economics_cycle: EconomicsCycle
    opportunity_cycle: OpportunityCycle
    maintenance_cycle: MaintenanceCycle
    credential_health_cycle: CredentialHealthCycle


def _build_messenger(bot_token: str | None, owner_chat_ids: tuple[int, ...]) -> MessengerPort:
    if not bot_token or not owner_chat_ids:
        return NullMessenger()
    # Import local: solo `ads-worker` carga `aiogram`/el bot real, y solo
    # cuando hay credenciales -- evita abrir sesion HTTP del SDK sin uso.
    from safent_ads.notifications.infrastructure.aiogram_messenger import (  # noqa: PLC0415
        AiogramMessenger,
    )

    return AiogramMessenger(bot_token=bot_token, owner_chat_ids=owner_chat_ids)


def build_default_runtime(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    platform_port: AdsPlatformPort,
    oauth_broker: OAuthBrokerPort,
    active_hours: ActiveHoursWindow,
    execution_use_cases_factory: ExecutionUseCasesFactory,
    telegram_bot_token: str | None = None,
    telegram_owner_chat_ids: tuple[int, ...] = (),
    digest_hour_default: int = 8,
    clock: Clock | None = None,
    id_generator: IdGenerator | None = None,
) -> OrchestrationRuntime:
    resolved_clock = clock or SystemClock()
    resolved_id_generator = id_generator or UuidIdGenerator()
    businesses = SqlBusinessListing(session_factory)
    messenger = _build_messenger(telegram_bot_token, telegram_owner_chat_ids)

    ingestion_cycle = IngestionCycle(
        businesses=businesses,
        ingestion_step=LiveIngestionStep(session_factory, platform_port, resolved_clock),
        clock=resolved_clock,
        id_generator=resolved_id_generator,
    )
    signal_cycle = SignalCycle(
        businesses=businesses,
        signal_step=LiveSignalStep(session_factory, resolved_clock),
        clock=resolved_clock,
        id_generator=resolved_id_generator,
    )
    rule_cycle = RuleCycle(
        businesses=businesses,
        rule_step=LiveRuleStep(session_factory, execution_use_cases_factory, resolved_clock),
        clock=resolved_clock,
        id_generator=resolved_id_generator,
    )
    economics_cycle = EconomicsCycle(
        businesses=businesses,
        economics_step=LiveEconomicsStep(session_factory, resolved_clock),
        clock=resolved_clock,
        id_generator=resolved_id_generator,
    )
    execution_cycle = ExecutionCycle(
        run_once=_build_execution_runner(session_factory, execution_use_cases_factory),
        clock=resolved_clock,
        id_generator=resolved_id_generator,
    )
    signal_outcome_cycle = SignalOutcomeCycle(
        businesses=businesses,
        signal_outcome_step=LiveSignalOutcomeStep(session_factory, resolved_clock),
        clock=resolved_clock,
        id_generator=resolved_id_generator,
    )
    rule_calibration_cycle = RuleCalibrationCycle(
        calibration_step=LiveRuleCalibrationStep(session_factory, resolved_clock),
        clock=resolved_clock,
        id_generator=resolved_id_generator,
    )
    opportunity_cycle = OpportunityCycle(
        businesses=businesses,
        opportunity_step=LiveOpportunityStep(session_factory, resolved_clock),
        clock=resolved_clock,
        id_generator=resolved_id_generator,
    )
    maintenance_cycle = MaintenanceCycle(
        businesses=businesses,
        maintenance_step=LiveMaintenanceStep(session_factory, resolved_clock),
        clock=resolved_clock,
        id_generator=resolved_id_generator,
    )
    credential_health_cycle = CredentialHealthCycle(
        businesses=businesses,
        credential_health_step=LiveCredentialHealthStep(
            session_factory,
            oauth_broker,
            PublishCredentialHealthAlert(
                outbox=_PerCallNotificationOutbox(session_factory),
                messenger=messenger,
                id_generator=resolved_id_generator,
            ),
            telegram_owner_chat_ids,
        ),
        clock=resolved_clock,
        id_generator=resolved_id_generator,
    )
    pending_digest = _PerCallPendingDigest(session_factory, active_hours, digest_hour_default)
    notification_cycle = NotificationCycle(
        targets=SqlNotificationTargets(session_factory, telegram_owner_chat_ids),
        publish_digest=PublishDigest(
            pending_digest=pending_digest,
            outbox=_PerCallNotificationOutbox(session_factory),
            messenger=messenger,
            id_generator=resolved_id_generator,
        ),
        publish_ticker=PublishTicker(
            signals=_PerCallSignalsForTicker(session_factory),
            outbox=_PerCallNotificationOutbox(session_factory),
            pending_digest=pending_digest,
            messenger=messenger,
            id_generator=resolved_id_generator,
            active_hours=active_hours,
        ),
        clock=resolved_clock,
        id_generator=resolved_id_generator,
    )

    return OrchestrationRuntime(
        ingestion_cycle,
        signal_cycle,
        rule_cycle,
        execution_cycle,
        notification_cycle,
        signal_outcome_cycle,
        rule_calibration_cycle,
        economics_cycle,
        opportunity_cycle,
        maintenance_cycle,
        credential_health_cycle,
    )


def _build_execution_runner(
    session_factory: async_sessionmaker[AsyncSession],
    execution_use_cases_factory: ExecutionUseCasesFactory,
) -> Callable[[], Awaitable[ExecutionStatus | None]]:
    """Una llamada = una sesion propia: `ExecutionChokepoint.run_once()`
    mas el `commit()` que le corresponde a quien abrio la sesion
    (`SqlUnitOfWork` solo confirma el tramo de reclamo/guardarraíl, ver su
    docstring)."""

    async def run_once() -> ExecutionStatus | None:
        async with session_factory() as session:
            use_cases = execution_use_cases_factory(session)
            outcome = await use_cases.chokepoint.run_once()
            await session.commit()
            return outcome

    return run_once


# ---------------------------------------------------------------------------
# `NotificationOutboxPort`/`PendingDigestPort`/`SignalsForTickerPort` reales
# necesitan una `AsyncSession` (los adaptadores de
# `notifications/infrastructure/sql_repositories.py` la piden en su
# constructor); estos envoltorios abren y confirman una por llamada, mismo
# patron que `RequestScopedPanelReadPort` en la lane de `panel`.
# ---------------------------------------------------------------------------


class _PerCallNotificationOutbox:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def try_reserve(self, notification: Notification) -> bool:
        async with self._session_factory() as session:
            return await SqlNotificationOutbox(session).try_reserve(notification)

    async def save(self, notification: Notification) -> None:
        async with self._session_factory() as session:
            await SqlNotificationOutbox(session).save(notification)


class _PerCallPendingDigest:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        active_hours: ActiveHoursWindow,
        digest_hour_default: int,
    ) -> None:
        self._session_factory = session_factory
        self._active_hours = active_hours
        self._digest_hour_default = digest_hour_default

    def _repository(self, session: AsyncSession) -> SqlPendingDigest:
        return SqlPendingDigest(
            session, active_hours=self._active_hours, digest_hour_default=self._digest_hour_default
        )

    async def enqueue(
        self, business_id: BusinessId, signals: list[TickerSignal], *, queued_at: datetime
    ) -> None:
        async with self._session_factory() as session:
            await self._repository(session).enqueue(business_id, signals, queued_at=queued_at)

    async def pop_due(self, business_id: BusinessId, *, at: datetime) -> list[TickerSignal]:
        async with self._session_factory() as session:
            return await self._repository(session).pop_due(business_id, at=at)


class _PerCallSignalsForTicker:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_actionable_signals(self, business_id: BusinessId) -> list[TickerSignal]:
        async with self._session_factory() as session:
            return await SqlSignalsForTicker(session).list_actionable_signals(business_id)
