"""`BeginOAuthConnect` (contracts/rest-api.md §Conexiones, paso 1, aplicado
a la conexion inicial): `POST /platform-accounts/connect/{provider}/start`.
Pide al broker el `state`/PKCE y solo persiste su version hasheada --
`ads-api` nunca guarda el `state` en claro, igual que un token de sesion.
El resultado nunca lleva el `state`: el panel solo recibe `session_id`
(correlacion de sondeo) y `authorize_url` (que ya lleva el `state` dentro,
camino a la plataforma, no de vuelta al cliente)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from safent_ads.accounts.application.connect_ports import (
    OAuthBrokerPort,
    OAuthConnectSessionRepository,
)
from safent_ads.accounts.application.oauth_state_hash import hash_state
from safent_ads.accounts.domain.google_customer_id import normalize_google_customer_id
from safent_ads.accounts.domain.oauth_connect_session import OAuthConnectSession
from safent_ads.shared.ids import BusinessId, IdGenerator, PlatformCode


@dataclass(frozen=True, slots=True, kw_only=True)
class BeginOAuthConnectResult:
    session_id: uuid.UUID
    authorize_url: str
    expires_at: datetime


class BeginOAuthConnect:
    def __init__(
        self,
        oauth_broker: OAuthBrokerPort,
        sessions: OAuthConnectSessionRepository,
        id_generator: IdGenerator,
    ) -> None:
        self._oauth_broker = oauth_broker
        self._sessions = sessions
        self._id_generator = id_generator

    async def execute(
        self,
        *,
        provider: PlatformCode,
        business_id: BusinessId,
        owner_id: uuid.UUID,
        redirect_uri: str,
        google_customer_id: str | None = None,
    ) -> BeginOAuthConnectResult:
        customer_id = normalize_google_customer_id(google_customer_id, provider=provider)
        selection = {"google_customer_id": customer_id} if customer_id is not None else {}
        broker_result = await self._oauth_broker.begin(
            provider, business_id, redirect_uri, owner_id=str(owner_id), **selection
        )
        session = OAuthConnectSession(
            session_id=self._id_generator.new_id(),
            business_id=business_id,
            owner_id=owner_id,
            provider=provider,
            state_hash=hash_state(broker_result.state),
            expires_at=broker_result.expires_at,
            connection_id=(
                uuid.UUID(broker_result.connection_id) if broker_result.connection_id else None
            ),
        )
        await self._sessions.save(session)
        return BeginOAuthConnectResult(
            session_id=session.session_id,
            authorize_url=broker_result.authorization_url,
            expires_at=broker_result.expires_at,
        )
