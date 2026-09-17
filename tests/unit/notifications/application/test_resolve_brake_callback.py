"""`ResolveBrakeCallback` (segundo toque de `/freno on|off`,
`brk:<nonce>:<y|n>`): `y` aplica la accion fijada en el primer toque, `n`
cancela sin tocar el freno, y un nonce caducado/consumido/desconocido
siempre responde "Caducada" (regla 1/3 de contracts/telegram.md, mismo
criterio que `ResolveCallback` para propuestas)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.notifications.application.resolve_brake_callback import ResolveBrakeCallback
from safent_ads.notifications.domain.brake_confirmation import (
    BrakeCallbackData,
    BrakeConfirmAction,
    PendingBrakeAction,
)
from safent_ads.notifications.testing.fakes import (
    FakeBrakeConfirmationStore,
    FakeBrakeGateway,
    FakeTelegramPairingGuard,
)
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)
_CHAT_ID = 111222333
_FROM_USER_ID = 111222333
_MESSAGE_ID = 999


def _build(
    *,
    gateway: FakeBrakeGateway | None = None,
    pairing_guard: FakeTelegramPairingGuard | None = None,
) -> tuple[ResolveBrakeCallback, FakeBrakeGateway, FakeBrakeConfirmationStore]:
    resolved_gateway = gateway or FakeBrakeGateway()
    confirmations = FakeBrakeConfirmationStore()
    resolver = ResolveBrakeCallback(
        confirmations=confirmations,
        gateway=resolved_gateway,
        pairing_guard=pairing_guard or FakeTelegramPairingGuard(),
        clock=FixedClock(_NOW),
    )
    return resolver, resolved_gateway, confirmations


async def _seed_confirmation(
    store: FakeBrakeConfirmationStore, *, action: PendingBrakeAction, nonce: str = "N" * 10
) -> str:
    await store.create(
        nonce=nonce, chat_id=_CHAT_ID, pending_action=action, expires_at=_NOW + timedelta(minutes=5)
    )
    return nonce


async def test_confirming_engage_applies_the_brake() -> None:
    resolver, gateway, store = _build()
    nonce = await _seed_confirmation(store, action=PendingBrakeAction.ENGAGE)
    data = BrakeCallbackData.build(nonce=nonce, action=BrakeConfirmAction.CONFIRM).encode()

    outcome = await resolver.execute(
        callback_data=data, chat_id=_CHAT_ID, from_user_id=_FROM_USER_ID, message_id=_MESSAGE_ID
    )

    assert gateway.engaged is True
    assert gateway.engage_calls == [f"telegram:{_FROM_USER_ID}"]
    assert "activado" in outcome.alert_text.lower()
    assert outcome.reply_markup == ()


async def test_confirming_release_applies_the_brake() -> None:
    gateway = FakeBrakeGateway()
    gateway.engaged = True
    resolver, gateway, store = _build(gateway=gateway)
    nonce = await _seed_confirmation(store, action=PendingBrakeAction.RELEASE)
    data = BrakeCallbackData.build(nonce=nonce, action=BrakeConfirmAction.CONFIRM).encode()

    outcome = await resolver.execute(
        callback_data=data, chat_id=_CHAT_ID, from_user_id=_FROM_USER_ID, message_id=_MESSAGE_ID
    )

    assert gateway.engaged is False
    assert gateway.release_calls == [f"telegram:{_FROM_USER_ID}"]
    assert "apagado" in outcome.alert_text.lower()


async def test_cancelling_leaves_the_brake_untouched() -> None:
    resolver, gateway, store = _build()
    nonce = await _seed_confirmation(store, action=PendingBrakeAction.ENGAGE)
    data = BrakeCallbackData.build(nonce=nonce, action=BrakeConfirmAction.CANCEL).encode()

    outcome = await resolver.execute(
        callback_data=data, chat_id=_CHAT_ID, from_user_id=_FROM_USER_ID, message_id=_MESSAGE_ID
    )

    assert gateway.engaged is False
    assert gateway.engage_calls == []
    assert outcome.edited_text == "Cancelado. El freno sigue igual."


async def test_unknown_or_consumed_nonce_replies_caducada() -> None:
    resolver, _gateway, _store = _build()
    data = BrakeCallbackData.build(nonce="Z" * 10, action=BrakeConfirmAction.CONFIRM).encode()

    outcome = await resolver.execute(
        callback_data=data, chat_id=_CHAT_ID, from_user_id=_FROM_USER_ID, message_id=_MESSAGE_ID
    )

    assert outcome.alert_text == "Caducada"


async def test_replaying_the_same_nonce_twice_only_applies_once() -> None:
    resolver, gateway, store = _build()
    nonce = await _seed_confirmation(store, action=PendingBrakeAction.ENGAGE)
    data = BrakeCallbackData.build(nonce=nonce, action=BrakeConfirmAction.CONFIRM).encode()

    first = await resolver.execute(
        callback_data=data, chat_id=_CHAT_ID, from_user_id=_FROM_USER_ID, message_id=_MESSAGE_ID
    )
    second = await resolver.execute(
        callback_data=data, chat_id=_CHAT_ID, from_user_id=_FROM_USER_ID, message_id=_MESSAGE_ID
    )

    assert first.alert_text != "Caducada"
    assert second.alert_text == "Caducada"
    assert gateway.engage_calls == [f"telegram:{_FROM_USER_ID}"]


async def test_allow_listed_but_unpaired_replies_sin_emparejar_without_a_decision() -> None:
    guard = FakeTelegramPairingGuard(paired=False)
    resolver, _gateway, store = _build(pairing_guard=guard)
    nonce = await _seed_confirmation(store, action=PendingBrakeAction.ENGAGE)
    data = BrakeCallbackData.build(nonce=nonce, action=BrakeConfirmAction.CONFIRM).encode()

    outcome = await resolver.execute(
        callback_data=data, chat_id=_CHAT_ID, from_user_id=_FROM_USER_ID, message_id=_MESSAGE_ID
    )

    assert outcome.alert_text == "Sin emparejar"
    assert guard.denied_chat_ids == []  # esa lista es de `ResolveCallback`, no de este resolutor


async def test_allow_listed_but_unpaired_leaves_a_decision_log_trail() -> None:
    """security-review-f4.md §2: a diferencia de `ResolveCallback`
    (`record_unpaired_callback`), este resolutor no dejaba NINGUNA
    constancia de la denegacion por falta de emparejamiento."""
    guard = FakeTelegramPairingGuard(paired=False)
    resolver, _gateway, store = _build(pairing_guard=guard)
    nonce = await _seed_confirmation(store, action=PendingBrakeAction.ENGAGE)
    data = BrakeCallbackData.build(nonce=nonce, action=BrakeConfirmAction.CONFIRM).encode()

    await resolver.execute(
        callback_data=data, chat_id=_CHAT_ID, from_user_id=_FROM_USER_ID, message_id=_MESSAGE_ID
    )

    assert guard.denied_commands == [(_CHAT_ID, "freno")]
