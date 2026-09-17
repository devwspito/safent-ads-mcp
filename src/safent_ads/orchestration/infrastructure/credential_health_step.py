"""`LiveCredentialHealthStep`: adaptador real de `CredentialHealthStepPort`
(tasks.md T126, threat-model.md C-21). Por negocio: pide a `accounts` el
chequeo de salud de sus credenciales (`CheckCredentialHealth` -- nunca
contacta Google/Meta, lee del broker via `OAuthBrokerPort`), anexa cada
transicion al `decision_log` en la misma transaccion que la persistencia
de `credential_refs`, y -- fuera de esa transaccion, best-effort como
cualquier otra entrega de este sistema (`notifications.application.
delivery`: "un fallo de entrega nunca bloquea el ciclo") -- alerta las que
exigen reconectar (`expiring_soon`/`expired`/`revoked`). Mismo criterio
que `LiveMaintenanceStep`: orchestration puede importar `accounts`/
`notifications`/`audit` directamente (plan.md §4, transversal)."""

from __future__ import annotations

from datetime import datetime
from typing import Final

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.application.check_credential_health import (
    CheckCredentialHealth,
    CredentialHealthTransition,
)
from safent_ads.accounts.application.connect_ports import OAuthBrokerPort
from safent_ads.accounts.domain.platform_credential import CredentialHealth
from safent_ads.accounts.infrastructure.sql_connect_repositories import SqlCredentialRepository
from safent_ads.accounts.infrastructure.sql_repositories import SqlAccountRepository
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.notifications.application.dto import CredentialHealthAlertEvent
from safent_ads.notifications.application.publish_credential_health_alert import (
    PublishCredentialHealthAlert,
)
from safent_ads.shared.ids import BusinessId, PlatformCode

logger = structlog.get_logger(__name__)

_ALERTABLE: Final[frozenset[CredentialHealth]] = frozenset(
    {CredentialHealth.EXPIRING_SOON, CredentialHealth.EXPIRED, CredentialHealth.REVOKED}
)
_PLATFORM_LABEL: Final[dict[PlatformCode, str]] = {
    PlatformCode.GOOGLE: "Google Ads",
    PlatformCode.META: "Meta Ads",
}
_HEALTH_LABEL: Final[dict[CredentialHealth, str]] = {
    CredentialHealth.EXPIRING_SOON: "caduca en menos de 7 dias",
    CredentialHealth.EXPIRED: "caducada",
    CredentialHealth.REVOKED: "revocada",
}
_SELECT_BUSINESS_NAME: Final = text("SELECT name FROM businesses WHERE id = :id")


class LiveCredentialHealthStep:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        oauth_broker: OAuthBrokerPort,
        publish_alert: PublishCredentialHealthAlert,
        owner_chat_ids: tuple[int, ...],
    ) -> None:
        self._session_factory = session_factory
        self._oauth_broker = oauth_broker
        self._publish_alert = publish_alert
        self._owner_chat_ids = owner_chat_ids

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None:
        del cycle_id
        business_name, transitions = await self._check_and_record(business_id, now)
        for transition in transitions:
            if transition.current_health in _ALERTABLE:
                await self._alert(business_name, transition, now)
        logger.info(
            "credential_health_checked", business_id=str(business_id), transitions=len(transitions)
        )

    async def _check_and_record(
        self, business_id: BusinessId, now: datetime
    ) -> tuple[str, list[CredentialHealthTransition]]:
        async with self._session_factory() as session:
            business_name = await self._business_name(session, business_id)
            use_case = CheckCredentialHealth(
                SqlAccountRepository(session), SqlCredentialRepository(session), self._oauth_broker
            )
            transitions = list(await use_case.execute(business_id, now=now))
            recorder = RecordDecision(SqlDecisionLogRepository(session))
            for transition in transitions:
                await recorder.execute(_to_pending_decision(transition))
            await session.commit()
        return business_name, transitions

    async def _business_name(self, session: AsyncSession, business_id: BusinessId) -> str:
        row = (
            await session.execute(_SELECT_BUSINESS_NAME, {"id": business_id.value})
        ).one_or_none()
        return row.name if row is not None else str(business_id)

    async def _alert(
        self, business_name: str, transition: CredentialHealthTransition, now: datetime
    ) -> None:
        event = CredentialHealthAlertEvent(
            business_name=business_name,
            account_ref=str(transition.account_ref),
            account_label=_account_label(transition),
            health_code=transition.current_health.value,
            health_label=_HEALTH_LABEL[transition.current_health],
            error_code=transition.error_code,
            occurred_at=now,
        )
        await self._publish_alert.execute(
            business_id=transition.business_id,
            owner_chat_ids=list(self._owner_chat_ids),
            event=event,
        )


def _account_label(transition: CredentialHealthTransition) -> str:
    platform_label = _PLATFORM_LABEL[transition.account_ref.platform]
    return f"{platform_label} ({transition.account_ref.external_account_id})"


def _to_pending_decision(transition: CredentialHealthTransition) -> PendingDecision:
    return PendingDecision(
        business_id=transition.business_id,
        kind=DecisionKind.CREDENTIAL_HEALTH_CHANGED,
        actor_kind=ActorKind.SYSTEM,
        payload={
            "platform": transition.account_ref.platform.value,
            "external_account_id": transition.account_ref.external_account_id,
            "previous_health": transition.previous_health.value,
            "current_health": transition.current_health.value,
            "error_code": transition.error_code,
        },
    )
