"""`PublishTicker` (plan.md §5, T042): ticker programado durante horario
activo; fuera de horario, delega en `PendingDigestPort` y no envia nada
ahora (contracts/telegram.md §Digest)."""

from __future__ import annotations

from datetime import datetime

from safent_ads.notifications.application.delivery import fan_out_to_owner_chats
from safent_ads.notifications.application.ports import (
    MessengerPort,
    NotificationOutboxPort,
    PendingDigestPort,
    SignalsForTickerPort,
)
from safent_ads.notifications.application.rendering import render_ticker_body
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.value_objects import ActiveHoursWindow, NotificationKind
from safent_ads.shared.ids import BusinessId, IdGenerator


class PublishTicker:
    """Un `execute()` por negocio y ciclo (orchestration.NotificationCycle
    es quien itera negocios y llama esto una vez por cada uno)."""

    def __init__(
        self,
        *,
        signals: SignalsForTickerPort,
        outbox: NotificationOutboxPort,
        pending_digest: PendingDigestPort,
        messenger: MessengerPort,
        id_generator: IdGenerator,
        active_hours: ActiveHoursWindow,
    ) -> None:
        self._signals = signals
        self._outbox = outbox
        self._pending_digest = pending_digest
        self._messenger = messenger
        self._id_generator = id_generator
        self._active_hours = active_hours

    async def execute(
        self,
        *,
        business_id: BusinessId,
        business_name: str,
        owner_chat_ids: list[int],
        now: datetime,
    ) -> list[Notification]:
        signals = await self._signals.list_actionable_signals(business_id)
        if not signals:
            return []

        if not self._active_hours.is_active(now):
            await self._pending_digest.enqueue(business_id, signals, queued_at=now)
            return []

        body = render_ticker_body(
            business_name=business_name,
            header_time_label=now.strftime("%H:%M"),
            signals=signals,
        )
        return await fan_out_to_owner_chats(
            business_id=business_id,
            kind=NotificationKind.TICKER,
            body=body,
            base_dedupe_key=f"ticker:{business_id}:{now:%Y-%m-%dT%H:%M}",
            owner_chat_ids=owner_chat_ids,
            outbox=self._outbox,
            messenger=self._messenger,
            id_generator=self._id_generator,
        )
