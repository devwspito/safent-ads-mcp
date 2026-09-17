"""Request-local identity; copied by asyncio.to_thread, never a fallback identity."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ConnectionScope:
    business_id: UUID
    connection_id: UUID


_CURRENT: ContextVar[ConnectionScope | None] = ContextVar("ads_connection_scope", default=None)


def current_connection_scope() -> ConnectionScope | None:
    return _CURRENT.get()


@contextmanager
def connection_scope(scope: ConnectionScope | None) -> Iterator[None]:
    token = _CURRENT.set(scope)
    try:
        yield
    finally:
        _CURRENT.reset(token)
