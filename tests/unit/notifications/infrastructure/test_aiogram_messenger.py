"""`AiogramMessenger` (T041): allow-list en ambos ids, `answerCallbackQuery`
siempre, escape HTML y reintento con espera creciente (contracts/telegram.md,
threat-model.md C-4)."""

from __future__ import annotations

import datetime

from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import CommandObject
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from safent_ads.notifications.application.ports import (
    CallbackOutcome,
    KeyboardButton,
    TelegramPairingConfirmationOutcome,
)
from safent_ads.notifications.infrastructure.aiogram_messenger import (
    AiogramMessenger,
    TelegramOwnerAllowList,
    UnimplementedCallbackResolver,
    UnimplementedPairingCommandResolver,
)
from tests.unit.notifications.infrastructure.fake_session import FakeSession

_APPROVE_BUTTON = (KeyboardButton(label="✅ Aprobar", callback_data="p:9f3a1c07:Kd7Qx2mVaP:a"),)

_OWNER_CHAT_ID = 111222333


def _messenger(**kwargs: object) -> tuple[AiogramMessenger, FakeSession]:
    session = FakeSession()
    messenger = AiogramMessenger(
        bot_token="123456:test-token",
        owner_chat_ids=[_OWNER_CHAT_ID],
        session=session,
        **kwargs,  # type: ignore[arg-type]
    )
    return messenger, session


def _message(*, chat_id: int, from_user_id: int | None) -> Message:
    chat = Chat(id=chat_id, type="private")
    from_user = (
        None if from_user_id is None else User(id=from_user_id, is_bot=False, first_name="Owner")
    )
    return Message(
        message_id=7, date=datetime.datetime.now(datetime.UTC), chat=chat, from_user=from_user
    )


def _command(args: str | None) -> CommandObject:
    return CommandObject(prefix="/", command="emparejar", mention=None, args=args)


def _callback_query(*, chat_id: int, from_user_id: int, data: str = "p:abc:xyz:a") -> CallbackQuery:
    chat = Chat(id=chat_id, type="private")
    message = Message(
        message_id=42, date=datetime.datetime.now(datetime.UTC), chat=chat
    )
    from_user = User(id=from_user_id, is_bot=False, first_name="Owner")
    return CallbackQuery(
        id="cbq-1",
        from_user=from_user,
        chat_instance="chat-instance-1",
        message=message,
        data=data,
    )


# --- TelegramOwnerAllowList: la comprobacion pura (threat-model.md C-4) ---


def test_reject_unknown_chat_id() -> None:
    allow_list = TelegramOwnerAllowList([_OWNER_CHAT_ID])

    assert allow_list.is_authorized(chat_id=999999999, from_user_id=_OWNER_CHAT_ID) is False


def test_reject_unknown_from_user_id_even_with_known_chat() -> None:
    """contracts/telegram.md: "Se comprueban ambos": un chat de grupo con
    el bot anadido no basta si quien pulsa no es el propietario."""
    allow_list = TelegramOwnerAllowList([_OWNER_CHAT_ID])

    assert allow_list.is_authorized(chat_id=_OWNER_CHAT_ID, from_user_id=999999999) is False


def test_accept_when_both_ids_match_owner() -> None:
    allow_list = TelegramOwnerAllowList([_OWNER_CHAT_ID])

    assert allow_list.is_authorized(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID) is True


def test_reject_when_no_message_attached() -> None:
    allow_list = TelegramOwnerAllowList([_OWNER_CHAT_ID])

    assert allow_list.is_authorized(chat_id=None, from_user_id=_OWNER_CHAT_ID) is False


# --- send/edit: escape HTML y timeout siempre presente ---


async def test_send_escapes_html_in_text() -> None:
    messenger, session = _messenger()

    await messenger.send(chat_id=_OWNER_CHAT_ID, text="<script>alert(1)</script> & cía")

    sent = session.requests[0]
    assert isinstance(sent, SendMessage)
    assert sent.text == "&lt;script&gt;alert(1)&lt;/script&gt; &amp; cía"


async def test_send_returns_message_id() -> None:
    messenger, _ = _messenger()

    message_id = await messenger.send(chat_id=_OWNER_CHAT_ID, text="hola")

    assert isinstance(message_id, int)


async def test_send_retries_on_rate_limit_then_succeeds() -> None:
    messenger, session = _messenger()
    send_method = SendMessage(chat_id=_OWNER_CHAT_ID, text="x")
    session.raise_sequence = [TelegramRetryAfter(send_method, "flood", retry_after=0)]

    message_id = await messenger.send(chat_id=_OWNER_CHAT_ID, text="hola")

    assert isinstance(message_id, int)
    assert len(session.requests) == 2  # 1 fallo + 1 exito


# --- teclados inline (contracts/telegram.md) ---


async def test_send_with_keyboard_includes_the_buttons() -> None:
    messenger, session = _messenger()

    await messenger.send(chat_id=_OWNER_CHAT_ID, text="hola", reply_markup=(_APPROVE_BUTTON,))

    sent = session.requests[0]
    assert isinstance(sent, SendMessage)
    assert sent.reply_markup is not None
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == "p:9f3a1c07:Kd7Qx2mVaP:a"
    assert sent.reply_markup.inline_keyboard[0][0].text == "✅ Aprobar"


async def test_send_without_keyboard_omits_reply_markup() -> None:
    messenger, session = _messenger()

    await messenger.send(chat_id=_OWNER_CHAT_ID, text="hola")

    assert session.requests[0].reply_markup is None


async def test_edit_with_empty_keyboard_removes_the_buttons() -> None:
    """contracts/telegram.md regla 4: "reply_markup vacío" al congelar un
    desenlace -- distinto de omitirlo (que Telegram interpreta como "no
    tocar el teclado que ya tuviera el mensaje")."""
    messenger, session = _messenger()

    await messenger.edit(chat_id=_OWNER_CHAT_ID, message_id=42, text="Aprobado", reply_markup=())

    edited = session.requests[0]
    assert isinstance(edited, EditMessageText)
    assert edited.reply_markup is not None
    assert edited.reply_markup.inline_keyboard == []


async def test_edit_without_keyboard_argument_does_not_touch_it() -> None:
    """`[Detalle]` no consume el nonce de aprobacion: reeditar el texto sin
    pasar `reply_markup` deja el teclado original intacto."""
    messenger, session = _messenger()

    await messenger.edit(chat_id=_OWNER_CHAT_ID, message_id=42, text="Detalle")

    assert session.requests[0].reply_markup is None


# --- callback_query: allow-list, answerCallbackQuery siempre ---


async def test_unauthorized_callback_gets_generic_rejection() -> None:
    messenger, session = _messenger()
    callback = _callback_query(chat_id=999999999, from_user_id=999999999).as_(messenger._bot)

    await messenger._handle_proposal_callback(callback)

    answer = session.requests[-1]
    assert isinstance(answer, AnswerCallbackQuery)
    assert answer.text == "No autorizado"
    assert answer.show_alert is True


async def test_authorized_but_unknown_nonce_is_treated_as_expired() -> None:
    """Sin resolutor real (US3 no cableada aun), toda pulsacion autorizada
    se trata como caducada — nunca como aprobada por defecto."""
    messenger, session = _messenger()
    callback = _callback_query(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(
        messenger._bot
    )

    await messenger._handle_proposal_callback(callback)

    answer = session.requests[-1]
    assert isinstance(answer, AnswerCallbackQuery)
    assert answer.text == "Caducada"
    assert answer.show_alert is False


async def test_callback_query_always_gets_an_answer() -> None:
    """contracts/telegram.md regla 3: "Siempre se llama a
    `answerCallbackQuery`" — autorizado o no."""
    for chat_id, from_user_id in [(999, 999), (_OWNER_CHAT_ID, _OWNER_CHAT_ID)]:
        messenger, session = _messenger()
        callback = _callback_query(chat_id=chat_id, from_user_id=from_user_id).as_(
            messenger._bot
        )

        await messenger._handle_proposal_callback(callback)

        assert any(isinstance(req, AnswerCallbackQuery) for req in session.requests)


async def test_custom_resolver_can_edit_the_message() -> None:
    class _ApprovingResolver:
        async def resolve(
            self,
            *,
            callback_data: str,  # noqa: ARG002 - firma exigida por `CallbackResolverPort`
            chat_id: int,  # noqa: ARG002 - idem
            from_user_id: int,  # noqa: ARG002 - idem
            message_id: int,  # noqa: ARG002 - idem
        ) -> CallbackOutcome:
            return CallbackOutcome(
                alert_text="Aprobado", show_alert=False, edited_text="✅ Aprobado"
            )

    messenger, session = _messenger(callback_resolver=_ApprovingResolver())
    callback = _callback_query(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(
        messenger._bot
    )

    await messenger._handle_proposal_callback(callback)

    edit_requests = [req for req in session.requests if req.__class__.__name__ == "EditMessageText"]
    assert len(edit_requests) == 1


async def test_resolver_receives_the_message_id_of_the_pressed_card() -> None:
    seen: dict[str, int] = {}

    class _RecordingResolver:
        async def resolve(
            self, *, callback_data: str, chat_id: int, from_user_id: int, message_id: int
        ) -> CallbackOutcome:
            del callback_data, chat_id, from_user_id
            seen["message_id"] = message_id
            return CallbackOutcome(alert_text="ok", show_alert=False)

    messenger, _session = _messenger(callback_resolver=_RecordingResolver())
    callback = _callback_query(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(
        messenger._bot
    )

    await messenger._handle_proposal_callback(callback)

    assert seen["message_id"] == 42


async def test_resolver_reply_markup_is_forwarded_to_the_edit() -> None:
    class _ReissuingResolver:
        async def resolve(
            self,
            *,
            callback_data: str,  # noqa: ARG002 - firma exigida por `CallbackResolverPort`
            chat_id: int,  # noqa: ARG002 - idem
            from_user_id: int,  # noqa: ARG002 - idem
            message_id: int,  # noqa: ARG002 - idem
        ) -> CallbackOutcome:
            return CallbackOutcome(
                alert_text="La propuesta cambió",
                show_alert=True,
                edited_text="tarjeta nueva",
                reply_markup=(_APPROVE_BUTTON,),
            )

    messenger, session = _messenger(callback_resolver=_ReissuingResolver())
    callback = _callback_query(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(
        messenger._bot
    )

    await messenger._handle_proposal_callback(callback)

    edited = next(req for req in session.requests if isinstance(req, EditMessageText))
    assert edited.reply_markup is not None
    assert edited.reply_markup.inline_keyboard[0][0].callback_data == "p:9f3a1c07:Kd7Qx2mVaP:a"


def test_unimplemented_resolver_is_the_default() -> None:
    messenger, _ = _messenger()

    assert isinstance(messenger._callback_resolver, UnimplementedCallbackResolver)


# --- long-polling: apagado ordenado sin pisar el SIGTERM/SIGINT del proceso ---


async def test_start_polling_disables_aiograms_own_signal_handlers() -> None:
    """El proceso que hospeda este mensajero (`ads-worker`) ya registra sus
    propios manejadores de SIGTERM/SIGINT; si aiogram tambien los
    registrase, se pisarian entre si (un unico manejador por senal)."""
    messenger, _session = _messenger()
    seen: dict[str, object] = {}

    async def _fake_start_polling(*_args: object, **kwargs: object) -> None:
        seen.update(kwargs)

    messenger._dispatcher.start_polling = _fake_start_polling  # type: ignore[method-assign]

    await messenger.start_polling()

    assert seen["handle_signals"] is False


async def test_stop_polling_delegates_to_the_dispatcher() -> None:
    messenger, _session = _messenger()
    called = False

    async def _fake_stop_polling() -> None:
        nonlocal called
        called = True

    messenger._dispatcher.stop_polling = _fake_stop_polling  # type: ignore[method-assign]

    await messenger.stop_polling()

    assert called is True


# --- `/emparejar <codigo>`: allow-list es la unica autorizacion (C-4) ---


async def test_pairing_command_rejects_unauthorized_chat() -> None:
    messenger, session = _messenger()
    message = _message(chat_id=999999999, from_user_id=999999999).as_(messenger._bot)

    await messenger._handle_pairing_command(message, _command("ABCD2345"))

    answer = session.requests[-1]
    assert isinstance(answer, SendMessage)
    assert answer.text == "No autorizado"


async def test_pairing_command_rejects_message_without_sender() -> None:
    messenger, session = _messenger()
    message = _message(chat_id=_OWNER_CHAT_ID, from_user_id=None).as_(messenger._bot)

    await messenger._handle_pairing_command(message, _command("ABCD2345"))

    answer = session.requests[-1]
    assert isinstance(answer, SendMessage)
    assert answer.text == "No autorizado"


async def test_pairing_command_never_calls_the_resolver_when_unauthorized() -> None:
    seen: list[str] = []

    class _RecordingPairingResolver:
        async def confirm(
            self, *, chat_id: int, code_text: str
        ) -> TelegramPairingConfirmationOutcome:
            del chat_id
            seen.append(code_text)
            return TelegramPairingConfirmationOutcome.CONFIRMED

    messenger, _session = _messenger(pairing_command_resolver=_RecordingPairingResolver())
    message = _message(chat_id=999999999, from_user_id=999999999).as_(messenger._bot)

    await messenger._handle_pairing_command(message, _command("ABCD2345"))

    assert seen == []


async def test_pairing_command_forwards_upper_cased_trimmed_code() -> None:
    seen: dict[str, str] = {}

    class _RecordingPairingResolver:
        async def confirm(
            self, *, chat_id: int, code_text: str
        ) -> TelegramPairingConfirmationOutcome:
            del chat_id
            seen["code_text"] = code_text
            return TelegramPairingConfirmationOutcome.CONFIRMED

    messenger, _session = _messenger(pairing_command_resolver=_RecordingPairingResolver())
    message = _message(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(messenger._bot)

    await messenger._handle_pairing_command(message, _command("  abcd2345  "))

    assert seen["code_text"] == "ABCD2345"


async def test_pairing_command_replies_with_the_outcome_text() -> None:
    class _StubResolver:
        async def confirm(
            self, *, chat_id: int, code_text: str
        ) -> TelegramPairingConfirmationOutcome:
            del chat_id, code_text
            return TelegramPairingConfirmationOutcome.RATE_LIMITED

    messenger, session = _messenger(pairing_command_resolver=_StubResolver())
    message = _message(chat_id=_OWNER_CHAT_ID, from_user_id=_OWNER_CHAT_ID).as_(messenger._bot)

    await messenger._handle_pairing_command(message, _command("ABCD2345"))

    answer = session.requests[-1]
    assert isinstance(answer, SendMessage)
    assert "una hora" in answer.text


def test_unimplemented_pairing_resolver_is_the_default() -> None:
    messenger, _ = _messenger()

    assert isinstance(messenger._pairing_command_resolver, UnimplementedPairingCommandResolver)
