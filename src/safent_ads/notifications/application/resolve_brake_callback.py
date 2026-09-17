"""`ResolveBrakeCallback` (este branch): resuelve el segundo toque de
`/freno on|off` (`brk:<nonce>:<y|n>`). `y` aplica la accion pendiente
(`PendingBrakeAction`, fijada en el primer toque, nunca en el boton); `n`
cancela sin tocar el freno. Mismas reglas 1/3/4 de contracts/telegram.md
que `ResolveCallback`: nonce caducado/consumido -> "Caducada"; siempre se
responde; el mensaje queda congelado sin teclado."""

from __future__ import annotations

from safent_ads.notifications.application.ports import (
    BrakeConfirmationStorePort,
    BrakeGatewayPort,
    CallbackOutcome,
    InlineKeyboard,
    TelegramPairingGuardPort,
)
from safent_ads.notifications.application.rendering import (
    render_brake_cancelled,
    render_brake_confirmation_expired,
    render_brake_toggle_outcome,
)
from safent_ads.notifications.domain.brake_confirmation import (
    BrakeCallbackData,
    BrakeConfirmAction,
    PendingBrakeAction,
)
from safent_ads.notifications.domain.errors import InvalidCallbackDataError
from safent_ads.shared.clock import Clock

_EMPTY_KEYBOARD: InlineKeyboard = ()
_UNPAIRED_OUTCOME = CallbackOutcome(alert_text="Sin emparejar", show_alert=True)
_STALE_OUTCOME = CallbackOutcome(
    alert_text="Caducada",
    show_alert=False,
    edited_text=render_brake_confirmation_expired(),
    reply_markup=_EMPTY_KEYBOARD,
)
_ENGAGE_REASON = "Freno de emergencia activado manualmente vía Telegram"
_RELEASE_REASON = "Freno de emergencia liberado manualmente vía Telegram"
_COMMAND_NAME = "freno"


class ResolveBrakeCallback:
    def __init__(
        self,
        *,
        confirmations: BrakeConfirmationStorePort,
        gateway: BrakeGatewayPort,
        pairing_guard: TelegramPairingGuardPort,
        clock: Clock,
    ) -> None:
        self._confirmations = confirmations
        self._gateway = gateway
        self._pairing_guard = pairing_guard
        self._clock = clock

    async def execute(
        self, *, callback_data: str, chat_id: int, from_user_id: int, message_id: int
    ) -> CallbackOutcome:
        del message_id  # el nonce de freno no se ata a un mensaje concreto
        if not await self._pairing_guard.is_chat_paired(chat_id):
            await self._pairing_guard.record_unpaired_command_denied(chat_id, command=_COMMAND_NAME)
            return _UNPAIRED_OUTCOME
        try:
            data = BrakeCallbackData.parse(callback_data)
        except InvalidCallbackDataError:
            return _STALE_OUTCOME
        record = await self._confirmations.consume(
            nonce=data.nonce, chat_id=chat_id, now=self._clock.now()
        )
        if record is None:
            return _STALE_OUTCOME
        if data.action is BrakeConfirmAction.CANCEL:
            return CallbackOutcome(
                alert_text="Cancelado",
                show_alert=False,
                edited_text=render_brake_cancelled(),
                reply_markup=_EMPTY_KEYBOARD,
            )
        return await self._apply(record.pending_action, engaged_by=f"telegram:{from_user_id}")

    async def _apply(self, action: PendingBrakeAction, *, engaged_by: str) -> CallbackOutcome:
        if action is PendingBrakeAction.ENGAGE:
            result = await self._gateway.engage(reason=_ENGAGE_REASON, engaged_by=engaged_by)
        else:
            result = await self._gateway.release(released_by=engaged_by)
        text = render_brake_toggle_outcome(result)
        return CallbackOutcome(
            alert_text=text, show_alert=False, edited_text=text, reply_markup=_EMPTY_KEYBOARD
        )
