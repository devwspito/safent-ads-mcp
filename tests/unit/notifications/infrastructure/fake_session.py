"""Sesion HTTP falsa para `Bot` de aiogram: nunca toca la red, deja
inspeccionar cada `TelegramMethod` enviado y forzar errores concretos para
probar el reintento de T041 sin `sleep` real."""

from __future__ import annotations

import datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.methods.base import TelegramMethod
from aiogram.types import Chat, Message


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.raise_sequence: list[Exception] = []

    async def close(self) -> None:
        return None

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def make_request(
        self,
        bot: Bot,  # noqa: ARG002 - firma exigida por `BaseSession.make_request`
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ARG002 - idem
    ) -> Any:
        self.requests.append(method)
        if self.raise_sequence:
            raise self.raise_sequence.pop(0)
        return self._canned_response(method)

    def _canned_response(self, method: TelegramMethod[Any]) -> Any:
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=datetime.datetime.now(datetime.UTC),
                chat=Chat(id=method.chat_id, type="private"),  # type: ignore[arg-type]
            )
        if isinstance(method, EditMessageText | AnswerCallbackQuery):
            return True
        raise NotImplementedError(method)
