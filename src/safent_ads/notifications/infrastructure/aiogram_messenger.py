"""`AiogramMessenger` (T041): implementa `MessengerPort` sobre aiogram 3 en
modo long-polling (plan.md §1: "sin webhook entrante"). Tambien resuelve
`callback_query` verificando la allow-list del propietario en `chat.id` Y
`from.id` (threat-model.md C-4) antes de delegar en `CallbackResolverPort`.

Escapa a HTML todo dato interpolado antes de enviar: el texto que compone
`notifications/application` puede contener nombres de campana, causas de
senal o insights de plataforma — datos de terceros no confiables
(threat-model.md §2 "Frontera A", amenaza de prompt injection/markup)."""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable, Iterable

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from safent_ads.notifications.application.errors import NotificationDeliveryError
from safent_ads.notifications.application.ports import (
    BrakeCallbackResolverPort,
    BrakeCommandReply,
    CallbackOutcome,
    CallbackResolverPort,
    InlineKeyboard,
    KeyboardButton,
    TelegramBrakeCommandPort,
    TelegramPairingCommandPort,
    TelegramPairingConfirmationOutcome,
    TelegramPendingCommandPort,
    TelegramStatusCommandPort,
)
from safent_ads.notifications.domain.pairing import mask_chat_id

logger = structlog.get_logger(__name__)

_CallbackResolverFn = Callable[..., Awaitable[CallbackOutcome]]

_MAX_DELIVERY_ATTEMPTS = 3
_BASE_BACKOFF_SECONDS = 1.0
_REQUEST_TIMEOUT_SECONDS = 10
_UNAUTHORIZED_ALERT = "No autorizado"
_UNKNOWN_COMMAND_REPLY = "Comando no reconocido. Usa /estado, /pendientes o /freno."
_PAIRING_COMMAND = "emparejar"
_ESTADO_COMMAND = "estado"
_PENDIENTES_COMMAND = "pendientes"
_FRENO_COMMAND = "freno"
_FRENO_ON_ARG = "on"
_FRENO_OFF_ARG = "off"
_BRAKE_CALLBACK_PREFIX = "brk:"
_PROPOSAL_CALLBACK_PREFIX = "p:"
_PAIRING_OUTCOME_REPLIES: dict[TelegramPairingConfirmationOutcome, str] = {
    TelegramPairingConfirmationOutcome.CONFIRMED: (
        "✅ Emparejado. Ya puedes recibir avisos y decidir desde aquí."
    ),
    TelegramPairingConfirmationOutcome.INVALID_CODE: "Código inválido. Formato: 8 letras/dígitos.",
    TelegramPairingConfirmationOutcome.NO_MATCH: "Código incorrecto o caducado.",
    TelegramPairingConfirmationOutcome.RATE_LIMITED: (
        "Demasiados intentos. Prueba de nuevo dentro de una hora."
    ),
}


def _has_prefix(prefix: str) -> Callable[[CallbackQuery], bool]:
    """Filtro de `callback_query` por prefijo de `callback_data`: cada
    familia de botones (propuestas `p:`, freno `brk:`) tiene su propio
    resolutor, y aiogram prueba los `Dispatcher.callback_query.register`
    en orden de alta hasta que uno encaja (contracts/telegram.md regla 3:
    de todos modos SIEMPRE se responde -- ver `_handle_unknown_callback`,
    el filtro final sin condicion)."""

    def matches(callback: CallbackQuery) -> bool:
        return (callback.data or "").startswith(prefix)

    return matches


def _looks_like_a_command(message: Message) -> bool:
    """Filtro final de mensajes: solo entra aqui lo que NINGUN `Command(...)`
    de arriba reconocio Y empieza por `/` (contracts/telegram.md: "unknown
    commands get one fixed reply"). Texto libre sin `/` no entra -- el bot
    simplemente no responde, que es "nunca acepta ordenes en texto libre"
    sin necesidad de decirlo cada vez."""
    return (message.text or "").startswith("/")


class TelegramOwnerAllowList:
    """Un origen es el propietario solo si `chat.id` **y** `from.id` estan
    ambos en la lista permitida (contracts/telegram.md: "Se comprueban
    ambos"). Puro, sin I/O: verificable sin levantar `Bot`/`Dispatcher`."""

    def __init__(self, owner_chat_ids: Iterable[int]) -> None:
        self._owner_chat_ids = frozenset(owner_chat_ids)

    def is_authorized(self, *, chat_id: int | None, from_user_id: int) -> bool:
        if chat_id is None:
            return False
        return chat_id in self._owner_chat_ids and from_user_id in self._owner_chat_ids


class UnimplementedCallbackResolver:
    """`CallbackResolverPort` por defecto hasta que US3 cablee la
    resolucion real de `CallbackNonce`: toda pulsacion se trata como
    caducada, nunca como aprobada (contracts/telegram.md regla 1)."""

    async def resolve(
        self, *, callback_data: str, chat_id: int, from_user_id: int, message_id: int
    ) -> CallbackOutcome:
        logger.info(
            "telegram_callback_not_yet_implemented",
            callback_data=callback_data,
            chat_id=chat_id,
            from_user_id=from_user_id,
            message_id=message_id,
        )
        return CallbackOutcome(alert_text="Caducada", show_alert=False)


class UnimplementedPairingCommandResolver:
    """`TelegramPairingCommandPort` por defecto: rechaza todo `/emparejar`
    como si no hiciera match (nunca confirma a ciegas)."""

    async def confirm(self, *, chat_id: int, code_text: str) -> TelegramPairingConfirmationOutcome:
        del chat_id, code_text
        return TelegramPairingConfirmationOutcome.NO_MATCH


_NOT_WIRED_REPLY = "No disponible todavía."


class UnimplementedStatusCommandResolver:
    """`TelegramStatusCommandPort` por defecto hasta que
    `telegram_channel.py` cablee `TelegramStatusCommandResolver`."""

    async def execute(self, *, chat_id: int) -> tuple[str, ...]:
        del chat_id
        return (_NOT_WIRED_REPLY,)


class UnimplementedPendingCommandResolver:
    """`TelegramPendingCommandPort` por defecto: no reenvia nada."""

    async def execute(self, *, chat_id: int) -> int:
        del chat_id
        return 0


class UnimplementedBrakeCommandResolver:
    """`TelegramBrakeCommandPort` por defecto: nunca emite un primer toque
    de verdad hasta que se cablee el resolutor real."""

    async def status(self, *, chat_id: int) -> BrakeCommandReply:
        del chat_id
        return BrakeCommandReply(text=_NOT_WIRED_REPLY)

    async def request_engage(self, *, chat_id: int) -> BrakeCommandReply:
        del chat_id
        return BrakeCommandReply(text=_NOT_WIRED_REPLY)

    async def request_release(self, *, chat_id: int) -> BrakeCommandReply:
        del chat_id
        return BrakeCommandReply(text=_NOT_WIRED_REPLY)


class UnimplementedBrakeCallbackResolver:
    """`BrakeCallbackResolverPort` por defecto: todo segundo toque de freno
    se trata como caducado, nunca como confirmado a ciegas (mismo criterio
    que `UnimplementedCallbackResolver`)."""

    async def resolve(
        self, *, callback_data: str, chat_id: int, from_user_id: int, message_id: int
    ) -> CallbackOutcome:
        del callback_data, chat_id, from_user_id, message_id
        return CallbackOutcome(alert_text="Caducada", show_alert=False)


def _to_aiogram_markup(keyboard: InlineKeyboard | None) -> InlineKeyboardMarkup | None:
    """`None` = no enviar el campo (Telegram conserva el teclado que ya
    tuviera el mensaje); `()` = enviarlo vacio de verdad (lo quita). Ambos
    casos son distintos y los dos hacen falta (contracts/telegram.md regla
    4 exige "reply_markup vacío" al congelar un desenlace; `[Detalle]`
    exige NO tocarlo porque "no consume el nonce de aprobacion")."""
    if keyboard is None:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[_to_aiogram_button(button) for button in row] for row in keyboard]
    )


def _to_aiogram_button(button: KeyboardButton) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=button.label, callback_data=button.callback_data)


async def _with_retry[T](operation: Callable[[], Awaitable[T]], *, description: str) -> T:
    last_error: Exception | None = None
    for attempt in range(1, _MAX_DELIVERY_ATTEMPTS + 1):
        try:
            return await operation()
        except TelegramRetryAfter as exc:
            last_error = exc
            logger.warning(
                "telegram_rate_limited", description=description, retry_after=exc.retry_after
            )
            await asyncio.sleep(exc.retry_after)
        except TelegramNetworkError as exc:
            last_error = exc
            logger.warning("telegram_network_error", description=description, attempt=attempt)
            await asyncio.sleep(_BASE_BACKOFF_SECONDS * attempt)
    raise NotificationDeliveryError(
        f"{description} fallo tras {_MAX_DELIVERY_ATTEMPTS} intentos"
    ) from last_error


class AiogramMessenger:
    """`MessengerPort` + resolutor de `callback_query` en un unico bot de
    long-polling (un solo `Bot` por proceso, contracts/telegram.md §1)."""

    def __init__(
        self,
        *,
        bot_token: str,
        owner_chat_ids: Iterable[int],
        callback_resolver: CallbackResolverPort | None = None,
        pairing_command_resolver: TelegramPairingCommandPort | None = None,
        status_command_resolver: TelegramStatusCommandPort | None = None,
        pending_command_resolver: TelegramPendingCommandPort | None = None,
        brake_command_resolver: TelegramBrakeCommandPort | None = None,
        brake_callback_resolver: BrakeCallbackResolverPort | None = None,
        session: BaseSession | None = None,
    ) -> None:
        """`session` es un punto de inyeccion para tests (`FakeSession`):
        en produccion, `Bot` crea su `AiohttpSession` real por defecto.

        `pending_command_resolver` casi siempre se cablea DESPUES, via
        `set_pending_command_resolver`: `/pendientes` necesita enviar por
        este mismo `Bot` (contracts/telegram.md §1: uno solo por proceso),
        y ese mismo `Bot` es quien registra el comando -- el resolutor no
        puede depender de este objeto mientras todavia se esta
        construyendo (`notifications/infrastructure/telegram_channel.py`
        rompe el ciclo asi)."""
        self._bot = Bot(
            token=bot_token,
            session=session,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        self._dispatcher = Dispatcher()
        self._allow_list = TelegramOwnerAllowList(owner_chat_ids)
        self._callback_resolver = callback_resolver or UnimplementedCallbackResolver()
        self._pairing_command_resolver = (
            pairing_command_resolver or UnimplementedPairingCommandResolver()
        )
        self._status_command_resolver = (
            status_command_resolver or UnimplementedStatusCommandResolver()
        )
        self._pending_command_resolver = (
            pending_command_resolver or UnimplementedPendingCommandResolver()
        )
        self._brake_command_resolver = (
            brake_command_resolver or UnimplementedBrakeCommandResolver()
        )
        self._brake_callback_resolver = (
            brake_callback_resolver or UnimplementedBrakeCallbackResolver()
        )
        self._dispatcher.callback_query.register(
            self._handle_proposal_callback, _has_prefix(_PROPOSAL_CALLBACK_PREFIX)
        )
        self._dispatcher.callback_query.register(
            self._handle_brake_callback, _has_prefix(_BRAKE_CALLBACK_PREFIX)
        )
        self._dispatcher.callback_query.register(self._handle_unknown_callback)
        self._dispatcher.message.register(self._handle_pairing_command, Command(_PAIRING_COMMAND))
        self._dispatcher.message.register(self._handle_estado_command, Command(_ESTADO_COMMAND))
        self._dispatcher.message.register(
            self._handle_pendientes_command, Command(_PENDIENTES_COMMAND)
        )
        self._dispatcher.message.register(self._handle_freno_command, Command(_FRENO_COMMAND))
        self._dispatcher.message.register(self._handle_unknown_command, _looks_like_a_command)

    def set_pending_command_resolver(self, resolver: TelegramPendingCommandPort) -> None:
        self._pending_command_resolver = resolver

    async def send(
        self,
        *,
        chat_id: int,
        text: str,
        disable_notification: bool = False,
        reply_markup: InlineKeyboard | None = None,
    ) -> int:
        message = await _with_retry(
            lambda: self._bot.send_message(
                chat_id=chat_id,
                text=html.escape(text),
                disable_notification=disable_notification,
                reply_markup=_to_aiogram_markup(reply_markup),
                request_timeout=_REQUEST_TIMEOUT_SECONDS,
            ),
            description="send_message",
        )
        return message.message_id

    async def edit(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboard | None = None,
    ) -> None:
        await _with_retry(
            lambda: self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=html.escape(text),
                reply_markup=_to_aiogram_markup(reply_markup),
                request_timeout=_REQUEST_TIMEOUT_SECONDS,
            ),
            description="edit_message_text",
        )

    async def _handle_proposal_callback(self, callback: CallbackQuery) -> None:
        await self._dispatch_callback(callback, resolver=self._callback_resolver.resolve)

    async def _handle_brake_callback(self, callback: CallbackQuery) -> None:
        await self._dispatch_callback(callback, resolver=self._brake_callback_resolver.resolve)

    async def _handle_unknown_callback(self, callback: CallbackQuery) -> None:
        """Regla 3 (contracts/telegram.md): SIEMPRE se responde, aunque el
        `callback_data` no encaje con ninguna familia conocida (boton de
        un mensaje viejo de una version anterior del bot, por ejemplo)."""
        await callback.answer(text="Caducada", show_alert=False)

    async def _dispatch_callback(
        self, callback: CallbackQuery, *, resolver: _CallbackResolverFn
    ) -> None:
        chat_id = callback.message.chat.id if callback.message is not None else None
        from_user_id = callback.from_user.id
        if chat_id is None or not self._allow_list.is_authorized(
            chat_id=chat_id, from_user_id=from_user_id
        ):
            await self._reject_unauthorized_callback(
                callback, chat_id=chat_id, from_user_id=from_user_id
            )
            return
        # `chat_id` solo llega no-None cuando `callback.message` tambien lo
        # es (comprobado arriba), asi que `message_id` es seguro aqui.
        message_id = callback.message.message_id  # type: ignore[union-attr]
        outcome = await resolver(
            callback_data=callback.data or "",
            chat_id=chat_id,
            from_user_id=from_user_id,
            message_id=message_id,
        )
        await callback.answer(text=outcome.alert_text, show_alert=outcome.show_alert)
        if outcome.edited_text is not None:
            await self.edit(
                chat_id=chat_id,
                message_id=message_id,
                text=outcome.edited_text,
                reply_markup=outcome.reply_markup,
            )

    async def _reject_unauthorized_callback(
        self, callback: CallbackQuery, *, chat_id: int | None, from_user_id: int
    ) -> None:
        # Identificadores enmascarados: un log nunca lleva el chat_id completo
        # (contracts/telegram.md, mismo criterio que el emparejamiento).
        logger.warning(
            "telegram_unauthorized_attempt",
            chat_id=mask_chat_id(chat_id) if chat_id is not None else None,
            from_user_id=mask_chat_id(from_user_id),
        )
        await callback.answer(text=_UNAUTHORIZED_ALERT, show_alert=True)

    def _authorized_message_chat_id(self, message: Message) -> int | None:
        """`None` si el origen no esta en la allow-list (ya deja constancia
        en el log, enmascarado) -- mismo criterio en las cuatro ordenes de
        texto (`/emparejar`, `/estado`, `/pendientes`, `/freno`)."""
        chat_id = message.chat.id
        from_user_id = message.from_user.id if message.from_user is not None else None
        if from_user_id is None or not self._allow_list.is_authorized(
            chat_id=chat_id, from_user_id=from_user_id
        ):
            logger.warning("telegram_unauthorized_attempt", chat_id_masked=mask_chat_id(chat_id))
            return None
        return chat_id

    async def _handle_pairing_command(self, message: Message, command: CommandObject) -> None:
        """`/emparejar <codigo>` (contracts/telegram.md §Emparejamiento):
        la allow-list es la UNICA autorizacion para invocar el comando --
        `TelegramPairingCommandPort.confirm` ya asume un `chat_id`
        autorizado y nunca la amplia (C-4)."""
        chat_id = self._authorized_message_chat_id(message)
        if chat_id is None:
            await message.answer(_UNAUTHORIZED_ALERT)
            return
        code_text = (command.args or "").strip().upper()
        outcome = await self._pairing_command_resolver.confirm(chat_id=chat_id, code_text=code_text)
        await message.answer(_PAIRING_OUTCOME_REPLIES[outcome])

    async def _handle_estado_command(self, message: Message) -> None:
        chat_id = self._authorized_message_chat_id(message)
        if chat_id is None:
            await message.answer(_UNAUTHORIZED_ALERT)
            return
        for card in await self._status_command_resolver.execute(chat_id=chat_id):
            await message.answer(card)

    async def _handle_pendientes_command(self, message: Message) -> None:
        chat_id = self._authorized_message_chat_id(message)
        if chat_id is None:
            await message.answer(_UNAUTHORIZED_ALERT)
            return
        await self._pending_command_resolver.execute(chat_id=chat_id)

    async def _handle_freno_command(self, message: Message, command: CommandObject) -> None:
        """`/freno`, `/freno on`, `/freno off` (contracts/telegram.md:
        "requiere segundo toque de confirmacion"). Ni `on` ni `off` aplican
        el cambio aqui -- solo emiten el primer toque."""
        chat_id = self._authorized_message_chat_id(message)
        if chat_id is None:
            await message.answer(_UNAUTHORIZED_ALERT)
            return
        arg = (command.args or "").strip().lower()
        reply = await self._resolve_freno_reply(chat_id=chat_id, arg=arg)
        await message.answer(
            reply.text, reply_markup=_to_aiogram_markup(reply.keyboard)
        )

    async def _resolve_freno_reply(self, *, chat_id: int, arg: str) -> BrakeCommandReply:
        if arg == "":
            return await self._brake_command_resolver.status(chat_id=chat_id)
        if arg == _FRENO_ON_ARG:
            return await self._brake_command_resolver.request_engage(chat_id=chat_id)
        if arg == _FRENO_OFF_ARG:
            return await self._brake_command_resolver.request_release(chat_id=chat_id)
        return BrakeCommandReply(text=_UNKNOWN_COMMAND_REPLY)

    async def _handle_unknown_command(self, message: Message) -> None:
        await message.answer(_UNKNOWN_COMMAND_REPLY)

    async def start_polling(self) -> None:
        """`handle_signals=False`: el proceso que hospeda este mensajero
        (contracts/telegram.md, decision documentada: `ads-worker`, no
        `ads-api` -- ver `notifications/infrastructure/telegram_channel.py`)
        ya registra sus propios manejadores de SIGTERM/SIGINT para el
        apagado ordenado de sus ciclos; si aiogram tambien los registrase,
        `asyncio.loop.add_signal_handler` los pisaria (un unico manejador
        por senal) y el apagado del resto del proceso dejaria de funcionar."""
        await self._dispatcher.start_polling(self._bot, handle_signals=False)

    async def stop_polling(self) -> None:
        await self._dispatcher.stop_polling()

    async def aclose(self) -> None:
        await self._bot.session.close()
