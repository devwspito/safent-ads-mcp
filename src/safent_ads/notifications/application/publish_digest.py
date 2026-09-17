"""`PublishDigest` (plan.md §5, T042): vacia la cola de senales acumuladas
fuera de horario activo en cuanto entra la primera hora activa
(contracts/telegram.md §Digest: "acumulado y enviado a la primera hora
activa"), con `disable_notification=true`."""

from __future__ import annotations

from datetime import datetime

from safent_ads.notifications.application.delivery import fan_out_to_owner_chats
from safent_ads.notifications.application.ports import (
    MessengerPort,
    NotificationOutboxPort,
    PendingDigestPort,
)
from safent_ads.notifications.application.rendering import render_ticker_body
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.value_objects import NotificationKind
from safent_ads.shared.ids import BusinessId, IdGenerator


class PublishDigest:
    def __init__(
        self,
        *,
        pending_digest: PendingDigestPort,
        outbox: NotificationOutboxPort,
        messenger: MessengerPort,
        id_generator: IdGenerator,
    ) -> None:
        self._pending_digest = pending_digest
        self._outbox = outbox
        self._messenger = messenger
        self._id_generator = id_generator

    async def execute(
        self,
        *,
        business_id: BusinessId,
        business_name: str,
        owner_chat_ids: list[int],
        now: datetime,
    ) -> list[Notification]:
        due = await self._pending_digest.pop_due(business_id, at=now)
        if not due:
            return []

        body = render_ticker_body(
            business_name=business_name,
            header_time_label=now.strftime("%H:%M"),
            signals=due,
        )
        return await fan_out_to_owner_chats(
            business_id=business_id,
            kind=NotificationKind.DIGEST,
            body=body,
            base_dedupe_key=f"digest:{business_id}:{now:%Y-%m-%dT%H:%M}",
            owner_chat_ids=owner_chat_ids,
            outbox=self._outbox,
            messenger=self._messenger,
            id_generator=self._id_generator,
            disable_notification=True,
        )
