"""Value objects de `notifications` (plan.md §5: `Notification` agregado con
`Channel`, `Severity` y `DeliveryState`; contracts/telegram.md para `Kind`)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from safent_ads.notifications.domain.errors import EmptyDedupeKeyError

_MAX_DEDUPE_KEY_LENGTH = 200
_ACTIVE_HOURS_PATTERN = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")


class Channel(StrEnum):
    """Canal de entrega. Solo Telegram en F1 (contracts/telegram.md); el
    tipo queda abierto a nuevos canales sin tocar el resto del agregado."""

    TELEGRAM = "telegram"


class Severity(StrEnum):
    """`INFO` se acumula en digest fuera de horario activo; `CRITICAL`
    interrumpe siempre (contracts/telegram.md)."""

    INFO = "info"
    CRITICAL = "critical"


class NotificationKind(StrEnum):
    """Formato de mensaje (contracts/telegram.md: Ticker, Digest, Critico,
    Recibo de accion autonoma, Solicitud de aprobacion)."""

    TICKER = "ticker"
    DIGEST = "digest"
    CRITICAL = "critical"
    AUTO_RECEIPT = "auto_receipt"
    APPROVAL_REQUEST = "approval_request"


class DeliveryState(StrEnum):
    """Ciclo de vida de entrega (contracts/telegram.md: "reintento ...
    maximo 3; luego queda DeliveryState.FAILED")."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DedupeKey:
    """Identidad estable de una notificacion logica: dos intentos de enviar
    "la misma" notificacion (mismo negocio, tipo y contenido) deben producir
    el mismo `DedupeKey`, para que un reintento nunca duplique el mensaje
    (NFR-6). La construccion determinista vive en cada caso de uso de
    `application/`, no aqui: este VO solo garantiza la forma valida."""

    value: str

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise EmptyDedupeKeyError("dedupe_key vacia")
        if len(self.value) > _MAX_DEDUPE_KEY_LENGTH:
            raise EmptyDedupeKeyError(
                f"dedupe_key excede {_MAX_DEDUPE_KEY_LENGTH} caracteres"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ActiveHoursWindow:
    """Ventana horaria activa (contracts/telegram.md §Digest: "fuera de
    horario activo, 08:00-21:00 Europe/Madrid"). Fuera de esta ventana, el
    ticker se acumula en digest; las senales `CRITICAL` la ignoran siempre."""

    start: time
    end: time
    tz: ZoneInfo

    @classmethod
    def parse(cls, raw: str, *, tz_name: str) -> ActiveHoursWindow:
        match = _ACTIVE_HOURS_PATTERN.match(raw)
        if match is None:
            raise ValueError(f"ventana de horario activo invalida: {raw!r}")
        start_h, start_m, end_h, end_m = (int(part) for part in match.groups())
        return cls(
            start=time(start_h, start_m),
            end=time(end_h, end_m),
            tz=ZoneInfo(tz_name),
        )

    def is_active(self, moment: datetime) -> bool:
        local = moment.astimezone(self.tz).time()
        return self.start <= local < self.end


@dataclass(frozen=True, slots=True)
class DigestSchedule:
    """Hora fija del digest diario (`businesses.digest_hour`,
    0023_owner_settings). Reemplaza "enviar en la primera hora activa": el
    digest ahora se libera una unica vez al dia, a esta hora exacta, en
    `tz`. Que solo se envie "una vez" no lo garantiza este VO -- lo
    garantiza que `pop_due` vacia la cola entera la primera vez que
    `is_due` da `True`; una segunda llamada dentro de la misma hora ya no
    encuentra nada pendiente que enviar."""

    hour: int
    tz: ZoneInfo

    def is_due(self, moment: datetime) -> bool:
        return moment.astimezone(self.tz).hour == self.hour
