"""`orchestration.infrastructure.runtime._build_messenger`: lo que
`composition/worker.py::run` cablea en `build_default_runtime`. Sin
credenciales de Telegram (composition/settings.py: opcionales al
arrancar), ads-worker nunca debe construir el bot real -- `NullMessenger`
en su lugar, mismo patron que `tests/unit/composition/test_broker.py`
prueba `_build_registry` directamente."""

from __future__ import annotations

from safent_ads.notifications.infrastructure.aiogram_messenger import AiogramMessenger
from safent_ads.notifications.infrastructure.null_messenger import NullMessenger
from safent_ads.orchestration.infrastructure.runtime import _build_messenger


def test_no_bot_token_yields_null_messenger() -> None:
    messenger = _build_messenger(None, (111222333,))

    assert isinstance(messenger, NullMessenger)


def test_no_owner_chat_ids_yields_null_messenger() -> None:
    messenger = _build_messenger("123456:test-bot-token", ())

    assert isinstance(messenger, NullMessenger)


def test_bot_token_and_owner_chat_ids_yield_the_real_aiogram_messenger() -> None:
    messenger = _build_messenger("123456:test-bot-token", (111222333,))

    assert isinstance(messenger, AiogramMessenger)
