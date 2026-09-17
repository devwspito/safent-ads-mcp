"""`OwnerTelegramChat` (data-model.md, FR-25): reglas puras del
emparejamiento Telegram<->propietario (contracts/telegram.md
§Emparejamiento con el panel). Este modulo no conoce la allow-list
`TELEGRAM_OWNER_CHAT_IDS` (C-4: "el emparejamiento no amplia la
allow-list") -- esa comprobacion vive en el adaptador del bot
(`notifications/infrastructure/aiogram_messenger.py`), no aqui."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from safent_ads.notifications.domain.errors import NotificationDomainError
from safent_ads.shared.ids import BusinessId

_CODE_PATTERN = re.compile(r"^[A-Z2-9]{8}$")
_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8

PAIRING_CODE_TTL = timedelta(minutes=10)
MAX_PAIRING_ATTEMPTS_PER_HOUR = 3
PAIRING_ATTEMPTS_WINDOW = timedelta(hours=1)

# security-review-f4.md item 1: `POST /telegram/pairing/test-message` no
# tenia freno -- mismo patron de conteo-en-ventana que `PAIRING_ATTEMPTS_WINDOW`.
MAX_TEST_MESSAGES_PER_WINDOW = 3
TEST_MESSAGE_RATE_LIMIT_WINDOW = timedelta(minutes=10)

# El emparejamiento no tiene `business_id` real (liga chat_id -> owner_id,
# nunca a un negocio, ver 0022_telegram_pairing): mismo patron centinela
# que `execution_rest.py::_ZERO_BUSINESS_ID` para el freno global, aqui
# para las entradas de `decision_log` de esta lane sin negocio al que
# atribuirlas.
UNSCOPED_BUSINESS_ID = BusinessId.parse("00000000-0000-0000-0000-000000000000")


class InvalidPairingCodeError(NotificationDomainError):
    """El codigo no cumple `^[A-Z2-9]{8}$` (contracts/telegram.md)."""


class PairingStatus(StrEnum):
    """`telegram_owner_chats.status` (data-model.md §OwnerTelegramChat)."""

    UNPAIRED = "unpaired"
    PENDING = "pending"
    PAIRED = "paired"


@dataclass(frozen=True, slots=True)
class PairingCode:
    """Codigo de emparejamiento de un solo uso (contracts/telegram.md:
    "^[A-Z2-9]{8}$"). El alfabeto excluye letras/digitos que el propietario
    podria confundir al teclearlo a mano no es una garantia de este VO --
    solo el patron del contrato importa aqui."""

    value: str

    def __post_init__(self) -> None:
        if not _CODE_PATTERN.fullmatch(self.value):
            raise InvalidPairingCodeError(f"codigo de emparejamiento invalido: {self.value!r}")

    @staticmethod
    def generate() -> PairingCode:
        return PairingCode("".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH)))

    def hash(self) -> str:
        return hashlib.sha256(self.value.encode("ascii")).hexdigest()


def mask_chat_id(chat_id: int) -> str:
    """Ultimos 4 digitos (rest-api.md `chat_id_masked`; repo rule: "no
    PII/chat ids in logs (mask)"). `abs()`: los chats de grupo de Telegram
    son negativos, y el signo no aporta nada a la mascara."""
    digits = str(abs(chat_id)).rjust(4, "0")
    return f"***{digits[-4:]}"


def effective_status(
    status: PairingStatus, *, code_expires_at: datetime | None, now: datetime
) -> PairingStatus:
    """`pending` con un codigo ya caducado se lee como `unpaired`: la fila
    en base no cambia sola (ningun proceso la purga todavia), pero ensenar
    un codigo muerto como "pendiente" seria mentirle al propietario."""
    if status is PairingStatus.PENDING and (code_expires_at is None or code_expires_at <= now):
        return PairingStatus.UNPAIRED
    return status
