"""`NullMessenger` (integracion): `MessengerPort` cuando Telegram no esta
configurado (`TELEGRAM_BOT_TOKEN`/`TELEGRAM_OWNER_CHAT_IDS` ausentes o
vacios). Nunca envia nada de verdad y nunca fail-open pretendiendo que
entrego: `send`/`edit` siempre lanzan `NotificationDeliveryError`, que
`notifications.application.delivery.deliver_and_record` ya sabe capturar
y marcar `FAILED` -- el ciclo sigue, la entrega queda registrada como lo
que es, nunca oculta."""

from __future__ import annotations

import structlog

from safent_ads.notifications.application.errors import NotificationDeliveryError
from safent_ads.notifications.application.ports import InlineKeyboard

logger = structlog.get_logger(__name__)


class NullMessenger:
    def __init__(self) -> None:
        self._warned = False

    async def send(
        self,
        *,
        chat_id: int,
        text: str,
        disable_notification: bool = False,
        reply_markup: InlineKeyboard | None = None,
    ) -> int:
        del chat_id, text, disable_notification, reply_markup
        self._warn_once()
        raise NotificationDeliveryError("notifications_disabled: Telegram no configurado")

    async def edit(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboard | None = None,
    ) -> None:
        del chat_id, message_id, text, reply_markup
        self._warn_once()
        raise NotificationDeliveryError("notifications_disabled: Telegram no configurado")

    def _warn_once(self) -> None:
        if self._warned:
            return
        self._warned = True
        logger.warning("notifications_disabled", reason="telegram_not_configured")
