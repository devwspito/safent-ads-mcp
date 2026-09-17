"""Errores propios del dominio `notifications` (shared/errors.py: "cada
bounded context define sus propias excepciones concretas")."""

from __future__ import annotations

from safent_ads.shared.errors import DomainError


class NotificationDomainError(DomainError):
    """Raiz de las violaciones de invariante del agregado `Notification`."""


class EmptyDedupeKeyError(NotificationDomainError):
    """`DedupeKey` vacia o solo espacios: no identifica nada de forma estable."""


class EmptyNotificationBodyError(NotificationDomainError):
    """Un `Notification` sin cuerpo no es enviable."""


class NotificationAlreadySentError(NotificationDomainError):
    """`mark_sent()` sobre una notificacion que ya esta `SENT`: la maquina de
    estados de entrega no permite reenviar la misma instancia (NFR-6)."""


class NotificationTerminalStateError(NotificationDomainError):
    """Transicion de estado pedida sobre una notificacion ya en estado
    terminal (`SENT`)."""


class InvalidCallbackDataError(NotificationDomainError):
    """`callback_data` con formato invalido o que supera 64 bytes
    (contracts/telegram.md §`callback_data`). Tratado siempre como una
    pulsacion no resoluble, nunca como una aprobacion."""
