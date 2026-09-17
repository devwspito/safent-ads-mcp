"""`/freno on` confirmado (segundo toque) enciende el freno de emergencia
por el MISMO camino que `POST /kill-switch`
(`composition/execution_rest.py:319-355` -> `ToggleEmergencyBrake`) contra
Postgres real: `TelegramBrakeCommandResolver`/`TelegramBrakeCallbackResolver`
construyen `ExecutionUseCases` con `Container.build_execution_use_cases`,
la fabrica que el REST tambien usa -- nunca un camino paralelo
(contracts/telegram.md: "Telegram debe llamar al mismo caso de uso que
REST")."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from safent_ads.composition.container import Container, ExecutionUseCases
from safent_ads.execution.domain.guardrails import BrakeMode, BrakeScope, BrakeScopeKind
from safent_ads.execution.infrastructure.sql_brake_state import SqlBrakeStatePort
from safent_ads.notifications.domain.brake_confirmation import (
    BrakeCallbackData,
    BrakeConfirmAction,
)
from safent_ads.notifications.infrastructure.telegram_command_resolvers import (
    TelegramBrakeCallbackResolver,
    TelegramBrakeCommandResolver,
)
from safent_ads.shared.clock import FixedClock
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_CHAT_ID = 111222333
_FROM_USER_ID = 111222333
_GLOBAL_SCOPE = BrakeScope(kind=BrakeScopeKind.GLOBAL)


@dataclass(frozen=True, slots=True)
class _TelegramBrakeFixture:
    container: Container
    command_resolver: TelegramBrakeCommandResolver
    callback_resolver: TelegramBrakeCallbackResolver

    async def current_brake(self) -> BrakeMode | None:
        async with self.container.session_factory() as session:
            brake = await SqlBrakeStatePort(session).get(_GLOBAL_SCOPE)
        return None if brake is None or not brake.engaged else brake.mode


@pytest.fixture
async def telegram_brake(isolated_database_url: str) -> AsyncIterator[_TelegramBrakeFixture]:
    """Cablea `TelegramBrakeCommandResolver`/`TelegramBrakeCallbackResolver`
    exactamente como `notifications/infrastructure/telegram_channel.py`,
    sobre un `Container` real -- misma fabrica de `ExecutionUseCases` que
    `composition/execution_rest.py`."""
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)

    def factory(session: object) -> ExecutionUseCases:
        return container.build_execution_use_cases(session)  # type: ignore[arg-type]

    # created_at uses Postgres NOW(): a historical fixed date eventually
    # violates expires_at > created_at, independently of the behavior tested.
    # Freeze once per fixture, aligned with the real database clock.
    clock = FixedClock(datetime.now(UTC))
    fixture = _TelegramBrakeFixture(
        container=container,
        command_resolver=TelegramBrakeCommandResolver(
            session_factory=container.session_factory,
            execution_use_cases_factory=factory,
            clock=clock,
        ),
        callback_resolver=TelegramBrakeCallbackResolver(
            session_factory=container.session_factory,
            execution_use_cases_factory=factory,
            clock=clock,
        ),
    )
    await _seed_paired_chat(container, chat_id=_CHAT_ID)
    try:
        yield fixture
    finally:
        await container.aclose()


async def _seed_paired_chat(container: Container, *, chat_id: int) -> None:
    """`ResolveBrakeCommand`/`ResolveBrakeCallback` exigen emparejamiento
    vivo (contracts/telegram.md: "sin fila verificada el canal avisa pero
    no decide") -- una fila de `telegram_owner_chats` en `status='paired'`
    real, commiteada, para que la resuelva `SqlTelegramPairingGuard` desde
    otra sesion. Idempotente: `isolated_database_url` es compartida entre
    los tests de este fichero (mismo `_CHAT_ID` en todos)."""
    async with container.session_factory() as session:
        already_paired = (
            await session.execute(
                text(
                    "SELECT 1 FROM telegram_owner_chats "
                    "WHERE chat_id = :chat_id AND status = 'paired'"
                ),
                {"chat_id": chat_id},
            )
        ).first()
        if already_paired is not None:
            return
        owner_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO owners (id, email, password_hash) "
                "VALUES (:id, :email, 'argon2id$fixture$not-a-real-hash')"
            ),
            {"id": owner_id, "email": f"owner-{owner_id.hex[:8]}@safent.example"},
        )
        await session.execute(
            text(
                "INSERT INTO telegram_owner_chats (owner_id, chat_id, status, verified_at) "
                "VALUES (:owner_id, :chat_id, 'paired', now())"
            ),
            {"owner_id": owner_id, "chat_id": chat_id},
        )
        await session.commit()


async def _tap_confirm(
    fixture: _TelegramBrakeFixture, reply_keyboard: object, *, message_id: int
) -> str:
    nonce = reply_keyboard[0][0].callback_data.split(":")[1]  # type: ignore[index]
    data = BrakeCallbackData.build(nonce=nonce, action=BrakeConfirmAction.CONFIRM).encode()
    outcome = await fixture.callback_resolver.resolve(
        callback_data=data, chat_id=_CHAT_ID, from_user_id=_FROM_USER_ID, message_id=message_id
    )
    return outcome.alert_text


async def test_freno_on_confirmed_engages_the_brake_via_the_rest_use_case(
    telegram_brake: _TelegramBrakeFixture,
) -> None:
    first_touch = await telegram_brake.command_resolver.request_engage(chat_id=_CHAT_ID)
    assert first_touch.keyboard is not None

    alert_text = await _tap_confirm(telegram_brake, first_touch.keyboard, message_id=1)

    assert "activado" in alert_text.lower()
    async with telegram_brake.container.session_factory() as session:
        brake = await SqlBrakeStatePort(session).get(_GLOBAL_SCOPE)
    assert brake is not None
    assert brake.engaged is True
    assert brake.mode is BrakeMode.ALL
    assert brake.reason == "Freno de emergencia activado manualmente vía Telegram"


async def test_freno_off_confirmed_releases_a_previously_engaged_brake(
    telegram_brake: _TelegramBrakeFixture,
) -> None:
    engage_touch = await telegram_brake.command_resolver.request_engage(chat_id=_CHAT_ID)
    assert engage_touch.keyboard is not None
    await _tap_confirm(telegram_brake, engage_touch.keyboard, message_id=0)

    release_touch = await telegram_brake.command_resolver.request_release(chat_id=_CHAT_ID)
    assert release_touch.keyboard is not None
    alert_text = await _tap_confirm(telegram_brake, release_touch.keyboard, message_id=1)

    assert "apagado" in alert_text.lower()
    async with telegram_brake.container.session_factory() as session:
        brake = await SqlBrakeStatePort(session).get(_GLOBAL_SCOPE)
    assert brake is not None
    assert brake.engaged is False


async def test_cancelling_the_second_touch_never_engages_the_brake(
    telegram_brake: _TelegramBrakeFixture,
) -> None:
    # `isolated_database_url` es compartida entre los tests de este fichero
    # (mismo ambito GLOBAL, un unico freno posible): se compara antes/
    # despues de cancelar en vez de asumir un estado de partida fijo, para
    # que el orden de ejecucion no importe.
    before_mode = await telegram_brake.current_brake()

    first_touch = await telegram_brake.command_resolver.request_engage(chat_id=_CHAT_ID)
    assert first_touch.keyboard is not None
    nonce = first_touch.keyboard[0][0].callback_data.split(":")[1]
    cancel_data = BrakeCallbackData.build(nonce=nonce, action=BrakeConfirmAction.CANCEL).encode()

    outcome = await telegram_brake.callback_resolver.resolve(
        callback_data=cancel_data, chat_id=_CHAT_ID, from_user_id=_FROM_USER_ID, message_id=1
    )

    assert outcome.edited_text == "Cancelado. El freno sigue igual."
    after_mode = await telegram_brake.current_brake()
    assert after_mode == before_mode


async def test_a_new_freno_request_invalidates_the_previous_second_touch(
    telegram_brake: _TelegramBrakeFixture,
) -> None:
    """Un solo segundo toque vivo por chat (revision F4 §2): pedir `/freno on`
    dos veces deja solo el ultimo nonce valido; el primero ya no engancha."""
    stale = await telegram_brake.command_resolver.request_engage(chat_id=_CHAT_ID)
    fresh = await telegram_brake.command_resolver.request_engage(chat_id=_CHAT_ID)
    assert stale.keyboard is not None and fresh.keyboard is not None

    stale_alert = await _tap_confirm(telegram_brake, stale.keyboard, message_id=1)
    async with telegram_brake.container.session_factory() as session:
        after_stale = await SqlBrakeStatePort(session).get(_GLOBAL_SCOPE)
    assert "activado" not in stale_alert.lower()
    assert after_stale is None or after_stale.engaged is False

    fresh_alert = await _tap_confirm(telegram_brake, fresh.keyboard, message_id=2)
    async with telegram_brake.container.session_factory() as session:
        after_fresh = await SqlBrakeStatePort(session).get(_GLOBAL_SCOPE)
    assert "activado" in fresh_alert.lower()
    assert after_fresh is not None and after_fresh.engaged is True
