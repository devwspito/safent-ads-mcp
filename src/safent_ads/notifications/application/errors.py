"""Errores de aplicacion de `notifications` (shared/errors.py:
`ApplicationError` = "fallo de un caso de uso: entidad no encontrada,
precondicion de puerto, etc.")."""

from __future__ import annotations

from safent_ads.shared.errors import ApplicationError


class NotificationDeliveryError(ApplicationError):
    """`MessengerPort.send`/`edit` agoto sus reintentos (contracts/telegram.md
    §Idempotencia y entrega: "maximo 3; luego queda DeliveryState.FAILED").
    Los casos de uso capturan esto puntualmente para marcar `FAILED` y
    seguir: una entrega fallida nunca bloquea el ciclo ni la caducidad."""


class TelegramAllowlistEmptyError(ApplicationError):
    """`POST /telegram/pairing/start` con `TELEGRAM_OWNER_CHAT_IDS` vacia
    (rest-api.md: "409 TELEGRAM_ALLOWLIST_EMPTY: es un paso de despliegue").
    """


class TelegramNotPairedError(ApplicationError):
    """`POST /telegram/pairing/test-message` sin un emparejamiento `paired`
    vigente (rest-api.md: "409 TELEGRAM_NOT_PAIRED")."""


class TelegramTestMessageRateLimitedError(ApplicationError):
    """`POST /telegram/pairing/test-message` supero el freno de
    `MAX_TEST_MESSAGES_PER_WINDOW` por `TEST_MESSAGE_RATE_LIMIT_WINDOW`
    (security-review-f4.md item 1: "429 RATE_LIMITED")."""
