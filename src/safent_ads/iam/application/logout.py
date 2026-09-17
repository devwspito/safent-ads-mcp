"""`Logout` (tasks.md T011): `POST /auth/logout` (contracts/rest-api.md).
Idempotente por diseno -- revocar una sesion ya ausente/revocada no es un
error, es el estado deseado."""

from __future__ import annotations

from safent_ads.iam.application.ports import SessionRepository
from safent_ads.iam.application.session_issuance import hash_session_token
from safent_ads.shared.clock import Clock


class Logout:
    def __init__(self, session_repository: SessionRepository, clock: Clock) -> None:
        self._sessions = session_repository
        self._clock = clock

    async def execute(self, *, raw_token: str) -> None:
        token_hash = hash_session_token(raw_token)
        session = await self._sessions.get_by_token_hash(token_hash)
        if session is None or session.is_revoked:
            return
        session.revoke(self._clock.now())
        await self._sessions.save(session)
