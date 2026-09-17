"""Entrega + registro compartidos por los tres casos de uso de publicacion
(`PublishTicker`, `PublishDigest`, `PublishCritical`): un fallo de entrega
nunca bloquea el ciclo (contracts/telegram.md §Idempotencia y entrega)."""

from __future__ import annotations

import structlog

from safent_ads.notifications.application.errors import NotificationDeliveryError
from safent_ads.notifications.application.ports import MessengerPort, NotificationOutboxPort
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


async def fan_out_to_owner_chats(
    *,
    business_id: BusinessId,
    kind: NotificationKind,
    body: str,
    base_dedupe_key: str,
    owner_chat_ids: list[int],
    outbox: NotificationOutboxPort,
    messenger: MessengerPort,
    id_generator: IdGenerator,
    disable_notification: bool = False,
) -> list[Notification]:
    """Construye, deduplica y entrega una notificacion por `chat_id`
    (contracts/telegram.md: el propietario puede recibir en varios chats
    permitidos; cada uno lleva su propio `dedupe_key`)."""
    sent: list[Notification] = []
    for chat_id in owner_chat_ids:
        notification = Notification(
            notification_id=id_generator.new_id(),
            business_id=business_id,
            channel=Channel.TELEGRAM,
            severity=Severity.INFO,
            kind=kind,
            dedupe_key=DedupeKey(f"{base_dedupe_key}:{chat_id}"),
            body=body,
        )
        if not await outbox.try_reserve(notification):
            continue
        await deliver_and_record(
            messenger=messenger,
            outbox=outbox,
            notification=notification,
            chat_id=chat_id,
            disable_notification=disable_notification,
        )
        sent.append(notification)
    return sent


async def deliver_and_record(
    *,
    messenger: MessengerPort,
    outbox: NotificationOutboxPort,
    notification: Notification,
    chat_id: int,
    disable_notification: bool = False,
) -> None:
    try:
        platform_message_id = await messenger.send(
            chat_id=chat_id,
            text=notification.body,
            disable_notification=disable_notification,
        )
    except NotificationDeliveryError:
        logger.warning(
            "notification_delivery_failed",
            notification_id=str(notification.notification_id),
            chat_id_masked=mask_chat_id(chat_id),
            kind=notification.kind.value,
        )
        notification.mark_failed(attempts=1)
    else:
        notification.mark_sent(platform_message_id=platform_message_id)
    await outbox.save(notification)
