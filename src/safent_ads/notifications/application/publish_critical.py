"""`PublishCritical` (plan.md §5, T042): interrumpe siempre, ignora el
horario activo y nunca se acumula en digest (contracts/telegram.md
§Critico)."""

from __future__ import annotations

import hashlib

from safent_ads.notifications.application.delivery import fan_out_to_owner_chats
from safent_ads.notifications.application.dto import CriticalEvent
from safent_ads.notifications.application.ports import MessengerPort, NotificationOutboxPort
from safent_ads.notifications.application.rendering import render_critical_body
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.value_objects import NotificationKind
from safent_ads.shared.ids import BusinessId, IdGenerator

_DEDUPE_TIME_BUCKET = "%Y-%m-%dT%H"  # por hora: no reabre la misma alerta cada ciclo


class PublishCritical:
    def __init__(
        self,
        *,
        outbox: NotificationOutboxPort,
        messenger: MessengerPort,
        id_generator: IdGenerator,
    ) -> None:
        self._outbox = outbox
        self._messenger = messenger
        self._id_generator = id_generator

    async def execute(
        self,
        *,
        business_id: BusinessId,
        owner_chat_ids: list[int],
        event: CriticalEvent,
    ) -> list[Notification]:
        body = render_critical_body(
            business_name=event.business_name,
            title=event.title,
            detail=event.detail,
            note=event.note,
            time_label=event.occurred_at.strftime("%H:%M"),
        )
        bucket = event.occurred_at.strftime(_DEDUPE_TIME_BUCKET)
        title_digest = hashlib.sha256(event.title.encode()).hexdigest()[:12]
        return await fan_out_to_owner_chats(
            business_id=business_id,
            kind=NotificationKind.CRITICAL,
            body=body,
            base_dedupe_key=f"critical:{business_id}:{title_digest}:{bucket}",
            owner_chat_ids=owner_chat_ids,
            outbox=self._outbox,
            messenger=self._messenger,
            id_generator=self._id_generator,
        )
