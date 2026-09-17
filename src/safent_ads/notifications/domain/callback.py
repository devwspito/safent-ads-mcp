"""`CallbackData` (contracts/telegram.md §`callback_data`): el sobre
`p:<proposal_id_short>:<nonce>:<action>` que viaja en cada boton inline,
≤64 bytes. Puro: parseo/formato/generacion de nonce, sin I/O de red ni de
base de datos -- la persistencia del nonce (`telegram_callbacks`) vive en
`notifications/application/ports.py::TelegramCallbackStorePort`."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from safent_ads.notifications.domain.errors import InvalidCallbackDataError

_MAX_CALLBACK_DATA_BYTES = 64
_NONCE_LENGTH = 10
_NONCE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_PROPOSAL_ID_SHORT_LENGTH = 8
_MAX_NONCE_TTL = timedelta(hours=6)

_CALLBACK_DATA_PATTERN = re.compile(
    r"^p:(?P<proposal_id_short>[0-9a-f]{8}):(?P<nonce>[A-Za-z0-9]{10}):(?P<action>[acrseu])$"
)


class CallbackAction(StrEnum):
    """`action` del sobre (contracts/telegram.md): letras fijas, nunca
    texto libre -- el bot no acepta ordenes fuera del teclado inline."""

    APPROVE = "a"
    CONFIRM = "c"
    REJECT = "r"
    SNOOZE = "s"
    DETAIL = "e"
    UNDO = "u"


def generate_nonce() -> str:
    """10 caracteres base62, aleatorio criptografico (contracts/telegram.md).
    `secrets.choice` usa el CSPRNG del sistema operativo, igual que
    `uuid.uuid4()` (ya usado como `IdGenerator` por defecto en `shared/ids.py`)."""
    return "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(_NONCE_LENGTH))


def short_proposal_id(proposal_id: str) -> str:
    """Primeros 8 hex del UUID (contracts/telegram.md: "primeros 8 hex del
    UUID"). Un UUID canonico empieza por su primer grupo de 8 caracteres
    hex antes del primer guion, asi que cortar la cadena basta."""
    return proposal_id.replace("-", "")[:_PROPOSAL_ID_SHORT_LENGTH]


def callback_ttl(*, proposal_expires_at: datetime, now: datetime) -> datetime:
    """TTL del nonce = `min(proposal.expires_at, now + 6h)` (contracts/
    telegram.md regla 5): un nonce nunca sobrevive a su propuesta ni supera
    el techo de 6 horas, aunque la propuesta caduque mucho mas tarde."""
    return min(proposal_expires_at, now + _MAX_NONCE_TTL)


@dataclass(frozen=True, slots=True)
class CallbackData:
    """El sobre parseado de un `callback_data` (contracts/telegram.md).
    `proposal_id_short` es opaco aqui: resolver la propuesta completa exige
    el nonce, que es quien de verdad liga la pulsacion a una fila de
    `telegram_callbacks` (y por tanto al UUID completo)."""

    proposal_id_short: str
    nonce: str
    action: CallbackAction

    def encode(self) -> str:
        raw = f"p:{self.proposal_id_short}:{self.nonce}:{self.action.value}"
        if len(raw.encode("ascii")) > _MAX_CALLBACK_DATA_BYTES:
            raise InvalidCallbackDataError(
                f"callback_data supera {_MAX_CALLBACK_DATA_BYTES} bytes: {raw!r}"
            )
        return raw

    @classmethod
    def build(cls, *, proposal_id: str, nonce: str, action: CallbackAction) -> CallbackData:
        return cls(proposal_id_short=short_proposal_id(proposal_id), nonce=nonce, action=action)

    @classmethod
    def parse(cls, raw: str) -> CallbackData:
        match = _CALLBACK_DATA_PATTERN.match(raw)
        if match is None:
            raise InvalidCallbackDataError(f"callback_data con formato invalido: {raw!r}")
        return cls(
            proposal_id_short=match.group("proposal_id_short"),
            nonce=match.group("nonce"),
            action=CallbackAction(match.group("action")),
        )
