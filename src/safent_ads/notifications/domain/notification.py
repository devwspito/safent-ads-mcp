"""Agregado `Notification` (plan.md §5, T040)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from safent_ads.notifications.domain.errors import (
    EmptyNotificationBodyError,
    NotificationAlreadySentError,
    NotificationTerminalStateError,
)
from safent_ads.notifications.domain.value_objects import (
    Channel,
    DedupeKey,
    DeliveryState,
    NotificationKind,
    Severity,
)
from safent_ads.shared.ids import BusinessId


@dataclass(slots=True)
class Notification:
    """Una notificacion saliente. `dedupe_key` identifica la notificacion
    *logica*: un reintento de publicacion que reconstruye el mismo
    `Notification` (mismo negocio, tipo y contenido) produce el mismo
    `dedupe_key`, y el puerto de salida (`MessengerPort`/outbox) es quien
    usa esa igualdad para no reenviar (NFR-6)."""

    notification_id: uuid.UUID
    business_id: BusinessId
    channel: Channel
    severity: Severity
    kind: NotificationKind
    dedupe_key: DedupeKey
    body: str
    delivery_state: DeliveryState = field(default=DeliveryState.PENDING)
    delivery_attempts: int = 0
    # `MessengerPort.send` devuelve el id de plataforma (integracion,
    # `notifications` tabla: CHECK "SENT exige sent_at y message_id" -- sin
    # este campo, un adaptador de outbox real no puede marcar SENT nunca).
    platform_message_id: int | None = None

    def __post_init__(self) -> None:
        if not self.body.strip():
            raise EmptyNotificationBodyError("body vacio")

    def mark_sent(self, *, platform_message_id: int | None = None) -> None:
        if self.delivery_state is DeliveryState.SENT:
            raise NotificationAlreadySentError(
                f"notification {self.notification_id} ya esta SENT"
            )
        self.delivery_state = DeliveryState.SENT
        self.platform_message_id = platform_message_id

    def mark_failed(self, *, attempts: int) -> None:
        """Fallo definitivo tras agotar los reintentos (contracts/telegram.md:
        "reintento ... maximo 3; luego queda DeliveryState.FAILED"). El conteo
        de reintentos vive en el adaptador de entrega (T041); aqui solo se
        registra el resultado final."""
        if self.delivery_state is DeliveryState.SENT:
            raise NotificationTerminalStateError(
                f"notification {self.notification_id} ya esta SENT, no puede fallar"
            )
        self.delivery_attempts = attempts
        self.delivery_state = DeliveryState.FAILED

    @property
    def is_terminal(self) -> bool:
        return self.delivery_state is DeliveryState.SENT
