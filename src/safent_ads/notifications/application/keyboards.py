"""Construccion de teclados inline (contracts/telegram.md): traduce accion +
nonce ya generados a `InlineKeyboard`. Puro -- no genera nonces ni persiste
nada, eso lo hace quien llama via `TelegramCallbackStorePort`."""

from __future__ import annotations

from safent_ads.notifications.application.ports import InlineKeyboard, KeyboardButton
from safent_ads.notifications.domain.brake_confirmation import (
    BrakeCallbackData,
    BrakeConfirmAction,
)
from safent_ads.notifications.domain.callback import CallbackAction, CallbackData


def _button(*, label: str, proposal_id: str, nonce: str, action: CallbackAction) -> KeyboardButton:
    data = CallbackData.build(proposal_id=proposal_id, nonce=nonce, action=action)
    return KeyboardButton(label=label, callback_data=data.encode())


def approval_request_keyboard(
    *,
    proposal_id: str,
    approve_nonce: str,
    reject_nonce: str,
    snooze_nonce: str,
    detail_nonce: str,
) -> InlineKeyboard:
    """Nivel 1 (contracts/telegram.md §Solicitud de aprobacion nivel 1):
    `[Aprobar][Detalle]` / `[Rechazar][24h]`."""
    return (
        (
            _button(
                label="✅ Aprobar",
                proposal_id=proposal_id,
                nonce=approve_nonce,
                action=CallbackAction.APPROVE,
            ),
            _button(
                label="🔍 Detalle",
                proposal_id=proposal_id,
                nonce=detail_nonce,
                action=CallbackAction.DETAIL,
            ),
        ),
        (
            _button(
                label="❌ Rechazar",
                proposal_id=proposal_id,
                nonce=reject_nonce,
                action=CallbackAction.REJECT,
            ),
            _button(
                label="⏸ 24 h",
                proposal_id=proposal_id,
                nonce=snooze_nonce,
                action=CallbackAction.SNOOZE,
            ),
        ),
    )


def confirm_spend_increase_keyboard(
    *, proposal_id: str, confirm_nonce: str, cancel_nonce: str
) -> InlineKeyboard:
    """Segundo toque (contracts/telegram.md): `[Sí, aprobar][Cancelar]`.
    Assumption documentada: "Cancelar" no tiene letra propia en el alfabeto
    cerrado de `action` (contracts/telegram.md §`callback_data`); se
    resuelve como `r` (rechazar) -- tocar "Cancelar" ante una subida de
    gasto es, en efecto, declinarla."""
    return (
        (
            _button(
                label="✅ Sí, aprobar",
                proposal_id=proposal_id,
                nonce=confirm_nonce,
                action=CallbackAction.CONFIRM,
            ),
            _button(
                label="✖️ Cancelar",
                proposal_id=proposal_id,
                nonce=cancel_nonce,
                action=CallbackAction.REJECT,
            ),
        ),
    )


def cause_group_keyboard(
    *, anchor_proposal_id: str, count: int, approve_all_nonce: str, detail_nonce: str
) -> InlineKeyboard:
    """Lote por causa (contracts/telegram.md §Lote por causa):
    `[Aprobar las N][Una a una]`."""
    return (
        (
            _button(
                label=f"✅ Aprobar las {count}",
                proposal_id=anchor_proposal_id,
                nonce=approve_all_nonce,
                action=CallbackAction.APPROVE,
            ),
            _button(
                label="🔍 Una a una",
                proposal_id=anchor_proposal_id,
                nonce=detail_nonce,
                action=CallbackAction.DETAIL,
            ),
        ),
    )


def brake_confirmation_keyboard(*, nonce: str) -> InlineKeyboard:
    """Segundo toque de `/freno on|off` (contracts/telegram.md): `[Confirmar]
    [Cancelar]`, mismo nonce para ambos botones -- el que sea que se pulse
    lo consume, y solo `y` aplica el cambio."""
    confirm = BrakeCallbackData.build(nonce=nonce, action=BrakeConfirmAction.CONFIRM)
    cancel = BrakeCallbackData.build(nonce=nonce, action=BrakeConfirmAction.CANCEL)
    return (
        (
            KeyboardButton(label="✅ Confirmar", callback_data=confirm.encode()),
            KeyboardButton(label="✖️ Cancelar", callback_data=cancel.encode()),
        ),
    )


def undo_keyboard(*, proposal_id: str, undo_nonce: str) -> InlineKeyboard:
    """Recibo de accion autonoma (contracts/telegram.md): `[↩️ Deshacer]`."""
    return (
        (
            _button(
                label="↩️ Deshacer",
                proposal_id=proposal_id,
                nonce=undo_nonce,
                action=CallbackAction.UNDO,
            ),
        ),
    )
