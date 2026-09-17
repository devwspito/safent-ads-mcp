"""`PublishApprovalRequest` (plan.md §5: "notifications: ... RequestApproval"):
envia la tarjeta de aprobacion nivel 1 (contracts/telegram.md §Solicitud de
aprobacion) y crea un nonce de un solo uso por boton. El nonce nace DESPUES
de `send()`: necesita el `message_id` real, que solo se conoce una vez
entregado el mensaje (`TelegramCallbackStorePort.create` explica por que).
Un fallo de entrega nunca bloquea el ciclo ni deja nonces huerfanos -- si
`send()` falla, no se crea ninguno."""

from __future__ import annotations

import structlog

from safent_ads.notifications.application.dto import ApprovalRequestView
from safent_ads.notifications.application.errors import NotificationDeliveryError
from safent_ads.notifications.application.keyboards import approval_request_keyboard
from safent_ads.notifications.application.ports import (
    MessengerPort,
    NotificationOutboxPort,
    TelegramCallbackStorePort,
)
from safent_ads.notifications.application.rendering import render_approval_request_card
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

_KEYBOARD_ACTIONS = (
    CallbackAction.APPROVE,
    CallbackAction.REJECT,
    CallbackAction.SNOOZE,
    CallbackAction.DETAIL,
)


class PublishApprovalRequest:
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
        self, *, business_id: BusinessId, owner_chat_ids: list[int], view: ApprovalRequestView
    ) -> list[Notification]:
        text = render_approval_request_card(view)
        sent: list[Notification] = []
        for chat_id in owner_chat_ids:
            dedupe_key = DedupeKey(
                f"approval_request:{view.proposal_id}:{view.diff_hash}:{chat_id}"
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
            await self._send_and_register(chat_id=chat_id, view=view, notification=notification)
            sent.append(notification)
        return sent

    async def _send_and_register(
        self, *, chat_id: int, view: ApprovalRequestView, notification: Notification
    ) -> None:
        nonces = {action: generate_nonce() for action in _KEYBOARD_ACTIONS}
        keyboard = approval_request_keyboard(
            proposal_id=view.proposal_id,
            approve_nonce=nonces[CallbackAction.APPROVE],
            reject_nonce=nonces[CallbackAction.REJECT],
            snooze_nonce=nonces[CallbackAction.SNOOZE],
            detail_nonce=nonces[CallbackAction.DETAIL],
        )
        try:
            message_id = await self._messenger.send(
                chat_id=chat_id, text=notification.body, reply_markup=keyboard
            )
        except NotificationDeliveryError:
            logger.warning(
                "approval_request_delivery_failed",
                proposal_id=view.proposal_id,
                chat_id_masked=mask_chat_id(chat_id),
            )
            notification.mark_failed(attempts=1)
            await self._outbox.save(notification)
            return
        notification.mark_sent(platform_message_id=message_id)
        await self._outbox.save(notification)
        await self._register_nonces(chat_id, message_id, view, nonces)

    async def _register_nonces(
        self,
        chat_id: int,
        message_id: int,
        view: ApprovalRequestView,
        nonces: dict[CallbackAction, str],
    ) -> None:
        expires_at = callback_ttl(proposal_expires_at=view.expires_at, now=self._clock.now())
        for action, nonce in nonces.items():
            await self._callback_store.create(
                nonce=nonce,
                proposal_id=view.proposal_id,
                chat_id=chat_id,
                message_id=message_id,
                diff_hash=view.diff_hash,
                action=action,
                expires_at=expires_at,
            )
