"""`PublishCauseGroupApproval` (contracts/telegram.md §Lote por causa):
misma mecanica que `PublishApprovalRequest`, pero el teclado solo lleva dos
botones (`[Aprobar las N]`/`[Una a una]`), ambos ligados al proposal_id
ANCLA -- `ResolveCallback.get_group_members` es quien resuelve el resto de
la causa en el momento de la pulsacion, no esta clase."""

from __future__ import annotations

import structlog

from safent_ads.notifications.application.dto import CauseGroupApprovalView
from safent_ads.notifications.application.errors import NotificationDeliveryError
from safent_ads.notifications.application.keyboards import cause_group_keyboard
from safent_ads.notifications.application.ports import (
    MessengerPort,
    NotificationOutboxPort,
    TelegramCallbackStorePort,
)
from safent_ads.notifications.application.rendering import render_cause_group_card
from safent_ads.notifications.domain.callback import CallbackAction, callback_ttl, generate_nonce
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.pairing import mask_chat_id
from safent_ads.notifications.domain.value_objects import (
    Channel,
    DedupeKey,
    NotificationKind,
    Severity,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, IdGenerator

logger = structlog.get_logger(__name__)


class PublishCauseGroupApproval:
    def __init__(
        self,
        *,
        outbox: NotificationOutboxPort,
        messenger: MessengerPort,
        callback_store: TelegramCallbackStorePort,
        id_generator: IdGenerator,
        clock: Clock,
    ) -> None:
        self._outbox = outbox
        self._messenger = messenger
        self._callback_store = callback_store
        self._id_generator = id_generator
        self._clock = clock

    async def execute(
        self, *, business_id: BusinessId, owner_chat_ids: list[int], group: CauseGroupApprovalView
    ) -> list[Notification]:
        text = render_cause_group_card(group)
        sent: list[Notification] = []
        for chat_id in owner_chat_ids:
            dedupe_key = DedupeKey(
                f"cause_group:{group.anchor_proposal_id}:{group.anchor_diff_hash}:{chat_id}"
            )
            notification = Notification(
                notification_id=self._id_generator.new_id(),
                business_id=business_id,
                channel=Channel.TELEGRAM,
                severity=Severity.INFO,
                kind=NotificationKind.APPROVAL_REQUEST,
                dedupe_key=dedupe_key,
                body=text,
            )
            if not await self._outbox.try_reserve(notification):
                continue
            await self._send_and_register(chat_id=chat_id, group=group, notification=notification)
            sent.append(notification)
        return sent

    async def _send_and_register(
        self, *, chat_id: int, group: CauseGroupApprovalView, notification: Notification
    ) -> None:
        approve_all_nonce = generate_nonce()
        detail_nonce = generate_nonce()
        keyboard = cause_group_keyboard(
            anchor_proposal_id=group.anchor_proposal_id,
            count=len(group.items),
            approve_all_nonce=approve_all_nonce,
            detail_nonce=detail_nonce,
        )
        try:
            message_id = await self._messenger.send(
                chat_id=chat_id, text=notification.body, reply_markup=keyboard
            )
        except NotificationDeliveryError:
            logger.warning(
                "cause_group_delivery_failed",
                anchor_proposal_id=group.anchor_proposal_id,
                chat_id=mask_chat_id(chat_id),
            )
            notification.mark_failed(attempts=1)
            await self._outbox.save(notification)
            return
        notification.mark_sent(platform_message_id=message_id)
        await self._outbox.save(notification)
        expires_at = callback_ttl(
            proposal_expires_at=group.items[0].expires_at, now=self._clock.now()
        )
        for action, nonce in (
            (CallbackAction.APPROVE, approve_all_nonce),
            (CallbackAction.DETAIL, detail_nonce),
        ):
            await self._callback_store.create(
                nonce=nonce,
                proposal_id=group.anchor_proposal_id,
                chat_id=chat_id,
                message_id=message_id,
                diff_hash=group.anchor_diff_hash,
                action=action,
                expires_at=expires_at,
            )
