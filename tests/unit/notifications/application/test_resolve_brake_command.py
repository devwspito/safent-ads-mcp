"""`ResolveBrakeCommand` (`/freno`, `/freno on`, `/freno off`): ninguna de
las tres rutas aplica el freno por si sola -- `on`/`off` solo emiten el
primer toque (contracts/telegram.md: "requiere segundo toque de
confirmacion")."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.notifications.application.resolve_brake_command import ResolveBrakeCommand
from safent_ads.notifications.domain.brake_confirmation import PendingBrakeAction
from safent_ads.notifications.testing.fakes import (
    FakeBrakeConfirmationStore,
    FakeBrakeGateway,
    FakeTelegramPairingGuard,
)
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)
_CHAT_ID = 111222333


def _build(
    *,
    gateway: FakeBrakeGateway | None = None,
    pairing_guard: FakeTelegramPairingGuard | None = None,
) -> tuple[ResolveBrakeCommand, FakeBrakeGateway, FakeBrakeConfirmationStore]:
    resolved_gateway = gateway or FakeBrakeGateway()
    confirmations = FakeBrakeConfirmationStore()
    resolver = ResolveBrakeCommand(
        gateway=resolved_gateway,
        confirmations=confirmations,
        pairing_guard=pairing_guard or FakeTelegramPairingGuard(),
        clock=FixedClock(_NOW),
    )
    return resolver, resolved_gateway, confirmations


async def test_status_authorized_and_paired_shows_the_brake_off() -> None:
    resolver, _gateway, _confirmations = _build()

    reply = await resolver.status(chat_id=_CHAT_ID)

    assert "apagado" in reply.text
    assert reply.keyboard is None


async def test_status_authorized_and_paired_shows_the_brake_engaged() -> None:
    gateway = FakeBrakeGateway()
    gateway.engaged = True
    gateway.mode = "all"
    resolver, _gateway, _confirmations = _build(gateway=gateway)

    reply = await resolver.status(chat_id=_CHAT_ID)

    assert "activado" in reply.text


async def test_request_engage_issues_a_confirmation_prompt_without_engaging() -> None:
    resolver, gateway, confirmations = _build()

    reply = await resolver.request_engage(chat_id=_CHAT_ID)

    assert "Confirmar" in reply.text
    assert reply.keyboard is not None
    assert gateway.engaged is False  # el primer toque nunca aplica el cambio
    nonce = reply.keyboard[0][0].callback_data.split(":")[1]
    record = await confirmations.consume(nonce=nonce, chat_id=_CHAT_ID, now=_NOW)
    assert record is not None
    assert record.pending_action is PendingBrakeAction.ENGAGE


async def test_request_release_issues_a_confirmation_prompt_without_releasing() -> None:
    gateway = FakeBrakeGateway()
    gateway.engaged = True
    resolver, _gateway, confirmations = _build(gateway=gateway)

    reply = await resolver.request_release(chat_id=_CHAT_ID)

    assert "Confirmar" in reply.text
    assert gateway.engaged is True
    nonce = reply.keyboard[0][0].callback_data.split(":")[1]  # type: ignore[index]
    record = await confirmations.consume(nonce=nonce, chat_id=_CHAT_ID, now=_NOW)
    assert record is not None
    assert record.pending_action is PendingBrakeAction.RELEASE


async def test_allow_listed_but_unpaired_replies_sin_emparejar_without_a_decision() -> None:
    guard = FakeTelegramPairingGuard(paired=False)
    resolver, _gateway, _confirmations = _build(pairing_guard=guard)

    status_reply = await resolver.status(chat_id=_CHAT_ID)
    engage_reply = await resolver.request_engage(chat_id=_CHAT_ID)
    release_reply = await resolver.request_release(chat_id=_CHAT_ID)

    assert status_reply.text == "Sin emparejar"
    assert engage_reply.text == "Sin emparejar"
    assert release_reply.text == "Sin emparejar"
    assert guard.denied_chat_ids == []
