"""Doble en memoria de `SessionByIdRepository`."""

from __future__ import annotations

import uuid
from datetime import datetime

from safent_ads.iam.domain.session import Session


class InMemorySessionRepository:
    def __init__(self) -> None:
        self._sessions_by_id: dict[uuid.UUID, Session] = {}

    async def create(self, session: Session) -> None:
        self._sessions_by_id[session.id] = session

    async def get_by_id(self, session_id: uuid.UUID) -> Session | None:
        return self._sessions_by_id.get(session_id)

    async def get_by_token_hash(self, token_hash: str) -> Session | None:
        for session in self._sessions_by_id.values():
            if session.token_hash == token_hash:
                return session
        return None

    async def save(self, session: Session) -> None:
        self._sessions_by_id[session.id] = session

    async def save_federated_mark(self, session_id: uuid.UUID, at: datetime) -> None:
        """Mismo contrato que `SqlSessionRepository.save_federated_mark`:
        via propia, ajena a `save()` (T064, re-verificacion de C-82)."""
        session = self._sessions_by_id.get(session_id)
        if session is not None:
            session.record_fresh_identification(at)
