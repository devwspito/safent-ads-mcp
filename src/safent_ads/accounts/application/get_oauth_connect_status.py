"""`GetOAuthConnectStatus`: `GET /platform-accounts/connect/{provider}/status
?session_id` (contracts/rest-api.md §Conexiones, paso 5: "conocer
session_id no autoriza" -- exige `business_id` de la sesion del
propietario, comprobado aqui contra `OAuthConnectSession.business_id`)."""

from __future__ import annotations

import uuid

from safent_ads.accounts.application.connect_ports import OAuthConnectSessionRepository
from safent_ads.accounts.application.errors import OAuthSessionNotFoundError
from safent_ads.accounts.domain.oauth_connect_session import OAuthConnectSession, OAuthSessionStatus
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class GetOAuthConnectStatus:
    def __init__(self, sessions: OAuthConnectSessionRepository, clock: Clock) -> None:
        self._sessions = sessions
        self._clock = clock

    async def execute(
        self, *, business_id: BusinessId, session_id: uuid.UUID
    ) -> OAuthConnectSession:
        session = await self._sessions.get_by_id(session_id)
        if session is None or session.business_id != business_id:
            raise OAuthSessionNotFoundError(str(session_id))
        if session.status == OAuthSessionStatus.WAITING and session.is_expired(self._clock.now()):
            # Callback completion owns this same row lock. Re-read after locking
            # so polling can never overwrite a committed success with expiry.
            session = await self._sessions.get_by_state_hash(session.state_hash)
            if session is None or session.business_id != business_id:
                raise OAuthSessionNotFoundError(str(session_id))
            if session.expire(at=self._clock.now()):
                await self._sessions.save(session)
        return session
