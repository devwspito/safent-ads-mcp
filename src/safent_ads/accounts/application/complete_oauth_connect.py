"""`CompleteOAuthConnect` (contracts/rest-api.md §Conexiones, pasos 3-4):
`GET /platform-accounts/connect/{provider}/callback`. Resuelve la sesion
por `state_hash` (un solo uso, TTL), entrega el `code` al broker sin
persistirlo, y crea/actualiza `PlatformAccount` + `credential_refs` por
cada cuenta descubierta -- nunca un token."""

from __future__ import annotations

from collections.abc import Sequence

from safent_ads.accounts.application._upsert_discovered_account import upsert_discovered_account
from safent_ads.accounts.application.connect_ports import (
    CredentialRepository,
    OAuthBrokerPort,
    OAuthConnectSessionRepository,
)
from safent_ads.accounts.application.errors import (
    BrokerRequestDeniedError,
    OAuthProviderDeniedError,
    OAuthSessionExpiredError,
    OAuthSessionNotFoundError,
)
from safent_ads.accounts.application.oauth_state_hash import hash_state
from safent_ads.accounts.application.ports import AccountRepository
from safent_ads.accounts.domain.errors import AccountOwnershipConflictError
from safent_ads.accounts.domain.oauth_connect_session import OAuthConnectSession, OAuthSessionStatus
from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import PlatformCode


class CompleteOAuthConnect:
    def __init__(
        self,
        oauth_broker: OAuthBrokerPort,
        sessions: OAuthConnectSessionRepository,
        accounts: AccountRepository,
        credentials: CredentialRepository,
        clock: Clock,
    ) -> None:
        self._oauth_broker = oauth_broker
        self._sessions = sessions
        self._accounts = accounts
        self._credentials = credentials
        self._clock = clock

    async def execute(
        self, *, state: str, code: str | None, provider: PlatformCode | None = None
    ) -> Sequence[PlatformAccount]:
        session = await self._require_valid_session(state)
        if provider is not None and session.provider != provider:
            raise OAuthSessionNotFoundError("state desconocido o ya consumido")
        if code is None:
            await self._fail_session(session, "OAUTH_CANCELLED")
            return []

        try:
            result = await self._oauth_broker.complete(state, code)
        except BrokerRequestDeniedError as exc:
            await self._fail_session(session, exc.error_code)
            raise OAuthProviderDeniedError(exc.error_code) from exc

        # A slow inventory must not admit accounts after the session TTL.
        if session.expire(at=self._clock.now()):
            await self._sessions.save(session)
            raise OAuthSessionExpiredError("sesion OAuth caducada")
        if not result.accounts:
            await self._fail_session(session, "OAUTH_NO_ACCESSIBLE_ACCOUNTS")
            raise OAuthProviderDeniedError("OAUTH_NO_ACCESSIBLE_ACCOUNTS")
        for discovered in result.accounts:
            if session.connection_id is not None and (
                discovered.connection_id != str(session.connection_id)
                or discovered.business_id != str(session.business_id)
                or discovered.owner_id != str(session.owner_id)
                or discovered.platform != session.provider
            ):
                raise AccountOwnershipConflictError("oauth_connection_identity_mismatch")
        accounts = [
            await upsert_discovered_account(
                self._accounts, self._credentials, session.business_id, discovered
            )
            for discovered in result.accounts
        ]
        session.mark_ok(at=self._clock.now())
        await self._sessions.save(session)
        return accounts

    async def _require_valid_session(self, state: str) -> OAuthConnectSession:
        session = await self._sessions.get_by_state_hash(hash_state(state))
        # `status != WAITING` cubre tanto una sesion ya resuelta (ok/error)
        # como un replay del `state`: en ambos casos, "desconocido o ya
        # consumido" es la unica respuesta que no filtra cual de los dos
        # ocurrio (OAuthConnectSession.mark_ok ya lo rechaza a nivel de
        # dominio; esto evita que ese InvalidStateTransitionError interno
        # se escape sin traducir).
        if session is None or session.status != OAuthSessionStatus.WAITING:
            raise OAuthSessionNotFoundError("state desconocido o ya consumido")
        if session.expire(at=self._clock.now()):
            await self._sessions.save(session)
            raise OAuthSessionExpiredError("sesion OAuth caducada")
        return session

    async def _fail_session(self, session: OAuthConnectSession, error_code: str) -> None:
        now = self._clock.now()
        if not session.expire(at=now):
            session.mark_error(error_code=error_code, at=now)
        await self._sessions.save(session)
