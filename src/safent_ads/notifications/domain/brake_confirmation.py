"""`BrakeConfirmationCallbackData` (contracts/telegram.md: "/freno on
requiere segundo toque de confirmacion"): el sobre `brk:<nonce>:<action>`
que viaja en el boton inline de confirmacion/cancelacion. Formato hermano
de `domain/callback.py::CallbackData`, pero sin ligar a una propuesta -- el
freno de emergencia es un interruptor global, no una entidad de
`proposals` (por eso vive en su propia tabla, `telegram_brake_confirmations`,
en vez de reusar `telegram_callbacks`, que exige `proposal_id NOT NULL`).

Puro: parseo/formato, sin I/O -- la persistencia del nonce vive en
`notifications/application/ports.py::BrakeConfirmationStorePort`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from safent_ads.notifications.domain.errors import InvalidCallbackDataError

_MAX_CALLBACK_DATA_BYTES = 64

_CALLBACK_DATA_PATTERN = re.compile(r"^brk:(?P<nonce>[A-Za-z0-9]{10}):(?P<action>y|n)$")


class PendingBrakeAction(StrEnum):
    """Lo que el primer toque de `/freno on|off` pidio confirmar -- se
    persiste junto al nonce para que el segundo toque nunca dependa de
    texto libre."""

    ENGAGE = "on"
    RELEASE = "off"


class BrakeConfirmAction(StrEnum):
    """Letra del boton pulsado en el segundo toque, nunca la accion de
    negocio en si (esa es `PendingBrakeAction`, ya fijada en el primer
    toque)."""

    CONFIRM = "y"
    CANCEL = "n"


@dataclass(frozen=True, slots=True)
class BrakeCallbackData:
    """El sobre parseado de un `callback_data` de confirmacion de freno."""

    nonce: str
    action: BrakeConfirmAction

    def encode(self) -> str:
        raw = f"brk:{self.nonce}:{self.action.value}"
        if len(raw.encode("ascii")) > _MAX_CALLBACK_DATA_BYTES:
            raise InvalidCallbackDataError(
                f"callback_data de freno supera {_MAX_CALLBACK_DATA_BYTES} bytes: {raw!r}"
            )
        return raw

    @classmethod
    def build(cls, *, nonce: str, action: BrakeConfirmAction) -> BrakeCallbackData:
        return cls(nonce=nonce, action=action)

    @classmethod
    def parse(cls, raw: str) -> BrakeCallbackData:
        match = _CALLBACK_DATA_PATTERN.match(raw)
        if match is None:
            raise InvalidCallbackDataError(f"callback_data de freno con formato invalido: {raw!r}")
        return cls(nonce=match.group("nonce"), action=BrakeConfirmAction(match.group("action")))
