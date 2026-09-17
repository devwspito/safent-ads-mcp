"""`PublishAutoReceipt` (plan.md §5): recibo de una accion autonoma ya
ejecutada, con `[↩️ Deshacer]` ligado a un nonce propio (contracts/telegram.md
§Recibo de accion autonoma). Igual que `PublishCritical`, esta pieza no
conoce a `execution`/`proposals`: recibe un `AutoReceiptEvent` ya
compuesto por quien orquesta el ciclo que ejecuta la accion (fuera de esta
lane, integracion)."""

from __future__ import annotations

import structlog

from safent_ads.notifications.application.dto import AutoReceiptEvent
from safent_ads.notifications.application.errors import NotificationDeliveryError
from safent_ads.notifications.application.keyboards import undo_keyboard
from safent_ads.notifications.application.ports import (
    MessengerPort,
    NotificationOutboxPort,
    TelegramCallbackStorePort,
)
from safent_ads.notifications.application.rendering import render_auto_receipt
from safent_ads.notifications.domain.callback import CallbackAction, generate_nonce
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.pairing import mask_chat_id
from safent_ads.notifications.domain.value_objects import (
    Channel,
    DedupeKey,
    NotificationKind,
    Severity,
)
from safent_ads.shared.ids import BusinessId, IdGenerator

logger = structlog.get_logger(__name__)


class PublishAutoReceipt:
    def __init__(
        self,
        *,
        outbox: NotificationOutboxPort,
        messenger: MessengerPort,
        callback_store: TelegramCallbackStorePort,
        id_generator: IdGenerator,
    ) -> None:
        self._outbox = outbox
        self._messenger = messenger
        self._callback_store = callback_store
        self._id_generator = id_generator

    async def execute(
        self, *, business_id: BusinessId, owner_chat_ids: list[int], event: AutoReceiptEvent
    ) -> list[Notification]:
        text = render_auto_receipt(event)
        sent: list[Notification] = []
        for chat_id in owner_chat_ids:
            dedupe_key = DedupeKey(f"auto_receipt:{event.proposal_id}:{event.diff_hash}:{chat_id}")
            notification = Notification(
                notification_id=self._id_generator.new_id(),
                business_id=business_id,
                channel=Channel.TELEGRAM,
                severity=Severity.INFO,
                kind=NotificationKind.AUTO_RECEIPT,
                dedupe_key=dedupe_key,
                body=text,
            )
            if not await self._outbox.try_reserve(notification):
                continue
            await self._send_and_register(chat_id=chat_id, event=event, notification=notification)
            sent.append(notification)
        return sent

    async def _send_and_register(
        self, *, chat_id: int, event: AutoReceiptEvent, notification: Notification
    ) -> None:
        undo_nonce = generate_nonce()
        keyboard = undo_keyboard(proposal_id=event.proposal_id, undo_nonce=undo_nonce)
        try:
            message_id = await self._messenger.send(
                chat_id=chat_id, text=notification.body, reply_markup=keyboard
            )
        except NotificationDeliveryError:
            logger.warning(
                "auto_receipt_delivery_failed",
                proposal_id=event.proposal_id,
                chat_id=mask_chat_id(chat_id),
            )
            notification.mark_failed(attempts=1)
            await self._outbox.save(notification)
            return
        notification.mark_sent(platform_message_id=message_id)
        await self._outbox.save(notification)
        await self._callback_store.create(
            nonce=undo_nonce,
            proposal_id=event.proposal_id,
            chat_id=chat_id,
            message_id=message_id,
            diff_hash=event.diff_hash,
            action=CallbackAction.UNDO,
            expires_at=event.undo_deadline,
        )
