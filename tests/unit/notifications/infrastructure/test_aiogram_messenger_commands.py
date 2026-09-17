"""`/estado`, `/pendientes`, `/freno` (este branch): la allow-list sigue
siendo la UNICA autorizacion para invocar cualquiera de los tres --
`_authorized_message_chat_id` es el MISMO helper que ya prueba
`test_pairing_command_rejects_unauthorized_chat` para `/emparejar` (mismo
log enmascarado, mismo criterio C-4), aqui se comprueba que las tres
ordenes nuevas lo reusan sin llamar nunca al resolutor real cuando el
origen no esta permitido."""

from __future__ import annotations

import datetime

from aiogram.filters import CommandObject
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from safent_ads.notifications.application.ports import (
    BrakeCommandReply,
    CallbackOutcome,
    KeyboardButton,
)
from safent_ads.notifications.infrastructure.aiogram_messenger import (
    AiogramMessenger,
    _has_prefix,
    _looks_like_a_command,
)
from tests.unit.notifications.infrastructure.fake_session import FakeSession

_OWNER_CHAT_ID = 111222333
_STRANGER_CHAT_ID = 999999999


def _messenger(**kwargs: object) -> tuple[AiogramMessenger, FakeSession]:
    session = FakeSession()
    messenger = AiogramMessenger(
        bot_token="123456:test-token",
        owner_chat_ids=[_OWNER_CHAT_ID],
        session=session,
        **kwargs,  # type: ignore[arg-type]
    )
    return messenger, session


def _message(*, chat_id: int, from_user_id: int | None, text: str | None = None) -> Message:
    chat = Chat(id=chat_id, type="private")
    from_user = (
        None if from_user_id is None else User(id=from_user_id, is_bot=False, first_name="Owner")
    )
    return Message(
        message_id=7,
        date=datetime.datetime.now(datetime.UTC),
        chat=chat,
        from_user=from_user,
        text=text,
    )


def _command(name: str, args: str | None = None) -> CommandObject:
    return CommandObject(prefix="/", command=name, mention=None, args=args)


def _callback_query(*, chat_id: int, from_user_id: int, data: str) -> CallbackQuery:
    chat = Chat(id=chat_id, type="private")
    message = Message(message_id=42, date=datetime.datetime.now(datetime.UTC), chat=chat)
    from_user = User(id=from_user_id, is_bot=False, first_name="Owner")
    return CallbackQuery(
        id="cbq-1", from_user=from_user, chat_instance="chat-instance-1", message=message, data=data
    )


class _RecordingStatusResolver:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def execute(self, *, chat_id: int) -> tuple[str, ...]:
        self.calls.append(chat_id)
        return ("📊 Negocio Uno", "📊 Negocio Dos")


class _RecordingPendingResolver:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def execute(self, *, chat_id: int) -> int:
        self.calls.append(chat_id)
        return 3


class _RecordingBrakeCommandResolver:
    def __init__(self) -> None:
        self.status_calls: list[int] = []
        self.engage_calls: list[int] = []
        self.release_calls: list[int] = []

    async def status(self, *, chat_id: int) -> BrakeCommandReply:
        self.status_calls.append(chat_id)
        return BrakeCommandReply(text="🟢 Freno: apagado")

    async def request_engage(self, *, chat_id: int) -> BrakeCommandReply:
        self.engage_calls.append(chat_id)
        return BrakeCommandReply(
            text="⚠️ Confirmar freno",
            keyboard=((KeyboardButton(label="✅ Confirmar", callback_data="brk:abc:y"),),),
        )

    async def request_release(self, *, chat_id: int) -> BrakeCommandReply:
        self.release_calls.append(chat_id)
        return BrakeCommandReply(text="⚠️ Confirmar freno")


class _RecordingBrakeCallbackResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(
        self, *, callback_data: str, chat_id: int, from_user_id: int, message_id: int
    ) -> CallbackOutcome:
        del chat_id, from_user_id, message_id
        self.calls.append(callback_data)
        return CallbackOutcome(alert_text="🔴 Freno activado.", show_alert=False)


# --- `/estado` ---


async def test_estado_authorized_sends_one_message_per_card() -> None:
    resolver = _RecordingStatusResolver()
    messenger, session = _messenger(status_command_resolver=resolver)
    message = _message(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(messenger._bot)

    await messenger._handle_estado_command(message)

    assert resolver.calls == [_OWNER_CHAT_ID]
    assert len(session.requests) == 2
    assert all(isinstance(request, SendMessage) for request in session.requests)


async def test_estado_unauthorized_never_calls_the_resolver() -> None:
    resolver = _RecordingStatusResolver()
    messenger, session = _messenger(status_command_resolver=resolver)
    message = _message(chat_id=_STRANGER_CHAT_ID, from_user_id=_STRANGER_CHAT_ID).as_(
        messenger._bot
    )

    await messenger._handle_estado_command(message)

    assert resolver.calls == []
    assert session.requests[-1].text == "No autorizado"


# --- `/pendientes` ---


async def test_pendientes_authorized_delegates_to_the_resolver() -> None:
    resolver = _RecordingPendingResolver()
    messenger, _session = _messenger(pending_command_resolver=resolver)
    message = _message(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(messenger._bot)

    await messenger._handle_pendientes_command(message)

    assert resolver.calls == [_OWNER_CHAT_ID]


async def test_pendientes_unauthorized_never_calls_the_resolver() -> None:
    resolver = _RecordingPendingResolver()
    messenger, session = _messenger(pending_command_resolver=resolver)
    message = _message(chat_id=_STRANGER_CHAT_ID, from_user_id=_STRANGER_CHAT_ID).as_(
        messenger._bot
    )

    await messenger._handle_pendientes_command(message)

    assert resolver.calls == []
    assert session.requests[-1].text == "No autorizado"


# --- `/freno`, `/freno on`, `/freno off` ---


async def test_freno_without_args_reports_status() -> None:
    resolver = _RecordingBrakeCommandResolver()
    messenger, session = _messenger(brake_command_resolver=resolver)
    message = _message(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(messenger._bot)

    await messenger._handle_freno_command(message, _command("freno"))

    assert resolver.status_calls == [_OWNER_CHAT_ID]
    assert session.requests[-1].text == "🟢 Freno: apagado"


async def test_freno_on_requests_engage_and_sends_the_confirmation_keyboard() -> None:
    resolver = _RecordingBrakeCommandResolver()
    messenger, session = _messenger(brake_command_resolver=resolver)
    message = _message(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(messenger._bot)

    await messenger._handle_freno_command(message, _command("freno", "on"))

    assert resolver.engage_calls == [_OWNER_CHAT_ID]
    assert resolver.release_calls == []
    sent = session.requests[-1]
    assert isinstance(sent, SendMessage)
    assert sent.reply_markup is not None
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == "brk:abc:y"


async def test_freno_off_requests_release() -> None:
    resolver = _RecordingBrakeCommandResolver()
    messenger, _session = _messenger(brake_command_resolver=resolver)
    message = _message(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(messenger._bot)

    await messenger._handle_freno_command(message, _command("freno", "off"))

    assert resolver.release_calls == [_OWNER_CHAT_ID]
    assert resolver.engage_calls == []


async def test_freno_with_an_invalid_argument_never_touches_the_brake() -> None:
    resolver = _RecordingBrakeCommandResolver()
    messenger, session = _messenger(brake_command_resolver=resolver)
    message = _message(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(messenger._bot)

    await messenger._handle_freno_command(message, _command("freno", "maybe"))

    assert resolver.status_calls == []
    assert resolver.engage_calls == []
    assert resolver.release_calls == []
    assert "no reconocido" in session.requests[-1].text.lower()


async def test_freno_unauthorized_never_calls_the_resolver() -> None:
    resolver = _RecordingBrakeCommandResolver()
    messenger, session = _messenger(brake_command_resolver=resolver)
    message = _message(chat_id=_STRANGER_CHAT_ID, from_user_id=_STRANGER_CHAT_ID).as_(
        messenger._bot
    )

    await messenger._handle_freno_command(message, _command("freno", "on"))

    assert resolver.engage_calls == []
    assert session.requests[-1].text == "No autorizado"


# --- comando desconocido: una unica respuesta fija ---


async def test_unknown_command_gets_the_fixed_reply() -> None:
    messenger, session = _messenger()
    message = _message(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(messenger._bot)

    await messenger._handle_unknown_command(message)

    assert "no reconocido" in session.requests[-1].text.lower()


# --- segundo toque de `/freno on|off`: `brk:` enruta a su propio resolutor ---


async def test_brake_callback_authorized_delegates_to_the_brake_resolver() -> None:
    resolver = _RecordingBrakeCallbackResolver()
    messenger, session = _messenger(brake_callback_resolver=resolver)
    callback = _callback_query(
        chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID, data="brk:abcdefghij:y"
    ).as_(messenger._bot)

    await messenger._handle_brake_callback(callback)

    assert resolver.calls == ["brk:abcdefghij:y"]
    assert isinstance(session.requests[-1], AnswerCallbackQuery)


async def test_brake_callback_unauthorized_never_calls_the_resolver() -> None:
    resolver = _RecordingBrakeCallbackResolver()
    messenger, _session = _messenger(brake_callback_resolver=resolver)
    callback = _callback_query(
        chat_id=_STRANGER_CHAT_ID, from_user_id=_STRANGER_CHAT_ID, data="brk:abcdefghij:y"
    ).as_(messenger._bot)

    await messenger._handle_brake_callback(callback)

    assert resolver.calls == []


async def test_unknown_callback_data_always_gets_an_answer() -> None:
    messenger, _session = _messenger()
    callback = _callback_query(
        chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID, data="garbage"
    ).as_(messenger._bot)

    # No lanza: contracts/telegram.md regla 3, "siempre se responde".
    await messenger._handle_unknown_callback(callback)


def test_has_prefix_filter_discriminates_proposal_and_brake_callbacks() -> None:
    proposal_callback = _callback_query(
        chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID, data="p:9f3a1c07:Kd7Qx2mVaP:a"
    )
    brake_callback = _callback_query(
        chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID, data="brk:abcdefghij:y"
    )

    assert _has_prefix("p:")(proposal_callback) is True
    assert _has_prefix("p:")(brake_callback) is False
    assert _has_prefix("brk:")(brake_callback) is True
    assert _has_prefix("brk:")(proposal_callback) is False


def test_looks_like_a_command_filter_only_matches_slash_prefixed_text() -> None:
    slash_message = _message(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID, text="/foobar")
    free_text_message = _message(
        chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID, text="activa el freno ya"
    )

    assert _looks_like_a_command(slash_message) is True
    assert _looks_like_a_command(free_text_message) is False
