"""`ResolveBrakeCommand` (este branch, `/freno`, `/freno on`, `/freno off`):
`/freno` sola lee el estado; `on`/`off` solo emiten el PRIMER toque -- el
segundo toque real que enciende o apaga el freno lo resuelve
`ResolveBrakeCallback` (contracts/telegram.md: "requiere segundo toque de
confirmacion"). Ninguna de las tres rutas de aqui llama a
`BrakeGatewayPort.engage`/`.release`."""

from __future__ import annotations

from datetime import timedelta

from safent_ads.notifications.application.keyboards import brake_confirmation_keyboard
from safent_ads.notifications.application.ports import (
    BrakeCommandReply,
    BrakeConfirmationStorePort,
    BrakeGatewayPort,
    TelegramPairingGuardPort,
)
from safent_ads.notifications.application.rendering import (
    render_brake_confirm_prompt,
    render_brake_status,
)
from safent_ads.notifications.domain.brake_confirmation import PendingBrakeAction
from safent_ads.notifications.domain.callback import generate_nonce
from safent_ads.shared.clock import Clock

_UNPAIRED_REPLY = BrakeCommandReply(text="Sin emparejar")
_CONFIRMATION_TTL = timedelta(minutes=5)


class ResolveBrakeCommand:
    def __init__(
        self,
        *,
        gateway: BrakeGatewayPort,
        confirmations: BrakeConfirmationStorePort,
        pairing_guard: TelegramPairingGuardPort,
        clock: Clock,
    ) -> None:
        self._gateway = gateway
        self._confirmations = confirmations
        self._pairing_guard = pairing_guard
        self._clock = clock

    async def status(self, *, chat_id: int) -> BrakeCommandReply:
        if not await self._pairing_guard.is_chat_paired(chat_id):
            return _UNPAIRED_REPLY
        return BrakeCommandReply(text=render_brake_status(await self._gateway.get_status()))

    async def request_engage(self, *, chat_id: int) -> BrakeCommandReply:
        return await self._request(chat_id=chat_id, action=PendingBrakeAction.ENGAGE)

    async def request_release(self, *, chat_id: int) -> BrakeCommandReply:
        return await self._request(chat_id=chat_id, action=PendingBrakeAction.RELEASE)

    async def _request(self, *, chat_id: int, action: PendingBrakeAction) -> BrakeCommandReply:
        if not await self._pairing_guard.is_chat_paired(chat_id):
            return _UNPAIRED_REPLY
        nonce = generate_nonce()
        await self._confirmations.create(
            nonce=nonce,
            chat_id=chat_id,
            pending_action=action,
            expires_at=self._clock.now() + _CONFIRMATION_TTL,
        )
        return BrakeCommandReply(
            text=render_brake_confirm_prompt(action),
            keyboard=brake_confirmation_keyboard(nonce=nonce),
        )
