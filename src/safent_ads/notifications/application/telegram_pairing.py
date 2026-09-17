"""Casos de uso del panel para `/telegram/pairing*` (rest-api.md
§Conexiones, Telegram y ajustes, FR-25): el propietario emite el codigo, lo
consulta mientras espera, lo desempareja y prueba el canal. La otra mitad
del flujo -- el bot resolviendo `/emparejar <codigo>` -- vive en
`confirm_telegram_pairing.py`, un actor distinto (el bot, no el panel) con
sus propias reglas de tasa."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from safent_ads.notifications.application.errors import (
    NotificationDeliveryError,
    TelegramAllowlistEmptyError,
    TelegramNotPairedError,
    TelegramTestMessageRateLimitedError,
)
from safent_ads.notifications.application.ports import (
    MessengerPort,
    PairingSnapshot,
    TelegramPairingRepositoryPort,
    TestMessageOutboxPort,
)
from safent_ads.notifications.domain.pairing import (
    MAX_TEST_MESSAGES_PER_WINDOW,
    PAIRING_CODE_TTL,
    TEST_MESSAGE_RATE_LIMIT_WINDOW,
    PairingCode,
    PairingStatus,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import IdGenerator

_TEST_MESSAGE_BODY = "🔔 Mensaje de prueba de Safent Ads. Si lo ves, el canal funciona."


class GetTelegramPairingStatus:
    def __init__(self, *, pairing: TelegramPairingRepositoryPort, clock: Clock) -> None:
        self._pairing = pairing
        self._clock = clock

    async def execute(self, owner_id: uuid.UUID) -> PairingSnapshot:
        snapshot = await self._pairing.get(owner_id, now=self._clock.now())
        return snapshot or _unpaired_snapshot()


@dataclass(frozen=True, slots=True)
class StartedPairing:
    pairing_code: str
    code_expires_at: datetime


class StartTelegramPairing:
    """`POST /telegram/pairing/start` (X-Reauth-Token, verificado por el
    presentador antes de llamar aqui: TOTP no es asunto de este caso de
    uso). `allowlist_configured` lo decide el proceso al arrancar
    (`TELEGRAM_OWNER_CHAT_IDS`), no cambia dentro de una peticion."""

    def __init__(
        self,
        *,
        pairing: TelegramPairingRepositoryPort,
        clock: Clock,
        allowlist_configured: bool,
    ) -> None:
        self._pairing = pairing
        self._clock = clock
        self._allowlist_configured = allowlist_configured

    async def execute(self, owner_id: uuid.UUID) -> StartedPairing:
        if not self._allowlist_configured:
            raise TelegramAllowlistEmptyError("TELEGRAM_OWNER_CHAT_IDS vacia")
        code = PairingCode.generate()
        expires_at = self._clock.now() + PAIRING_CODE_TTL
        await self._pairing.start_pairing(owner_id=owner_id, code=code, expires_at=expires_at)
        return StartedPairing(pairing_code=code.value, code_expires_at=expires_at)


class UnpairTelegram:
    """`DELETE /telegram/pairing`: idempotente por diseno (rest-api.md no
    distingue "ya estaba unpaired" de "se acaba de desemparejar")."""

    def __init__(self, *, pairing: TelegramPairingRepositoryPort) -> None:
        self._pairing = pairing

    async def execute(self, owner_id: uuid.UUID) -> None:
        await self._pairing.unpair(owner_id)


class SendTelegramTestMessage:
    """`POST /telegram/pairing/test-message`: encola en
    `telegram_test_messages`, nunca habla con Telegram (eso lo hace
    `ads-worker`, `DeliverTelegramTestMessages`)."""

    def __init__(
        self,
        *,
        pairing: TelegramPairingRepositoryPort,
        outbox: TestMessageOutboxPort,
        id_generator: IdGenerator,
        clock: Clock,
    ) -> None:
        self._pairing = pairing
        self._outbox = outbox
        self._id_generator = id_generator
        self._clock = clock

    async def execute(self, owner_id: uuid.UUID) -> uuid.UUID:
        snapshot = await self._pairing.get(owner_id, now=self._clock.now())
        chat_id = _paired_chat_id(snapshot)
        if chat_id is None:
            raise TelegramNotPairedError("sin emparejamiento vigente")
        now = self._clock.now()
        since = now - TEST_MESSAGE_RATE_LIMIT_WINDOW
        recent = await self._outbox.count_recent(owner_id, since=since)
        if recent >= MAX_TEST_MESSAGES_PER_WINDOW:
            raise TelegramTestMessageRateLimitedError("limite de mensajes de prueba alcanzado")
        notification_id = self._id_generator.new_id()
        await self._outbox.enqueue(
            notification_id=notification_id,
            owner_id=owner_id,
            chat_id=chat_id,
            body=_TEST_MESSAGE_BODY,
            at=now,
        )
        await self._pairing.record_test_message_sent(owner_id, at=now)
        return notification_id


class DeliverTelegramTestMessages:
    """Drena `telegram_test_messages` (llamado desde `ads-worker`, el unico
    proceso con un `MessengerPort` real conectado -- ver
    `notifications/infrastructure/telegram_channel.py`)."""

    def __init__(self, *, outbox: TestMessageOutboxPort, messenger: MessengerPort) -> None:
        self._outbox = outbox
        self._messenger = messenger

    async def execute(self, *, limit: int = 20) -> int:
        pending = await self._outbox.claim_pending(limit=limit)
        delivered = 0
        for task in pending:
            try:
                platform_message_id = await self._messenger.send(
                    chat_id=task.chat_id, text=task.body
                )
            except NotificationDeliveryError:
                await self._outbox.mark_failed(task.notification_id)
                continue
            await self._outbox.mark_sent(
                task.notification_id, platform_message_id=platform_message_id
            )
            delivered += 1
        return delivered


def _paired_chat_id(snapshot: PairingSnapshot | None) -> int | None:
    if snapshot is None or snapshot.status is not PairingStatus.PAIRED:
        return None
    return snapshot.chat_id


def _unpaired_snapshot() -> PairingSnapshot:
    return PairingSnapshot(
        status=PairingStatus.UNPAIRED,
        chat_id=None,
        paired_at=None,
        code_expires_at=None,
        pairing_code=None,
        last_test_at=None,
    )
