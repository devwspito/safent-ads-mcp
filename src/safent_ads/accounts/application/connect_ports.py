"""Puertos del flujo OAuth "Conectar" (US3, contracts/rest-api.md
§Conexiones). Separado de `ports.py` (`AdsPlatformPort`, US1) porque es
otro caso de uso con otro ciclo de vida: lectura/escritura de inventario
frente a alta de credenciales.

`OAuthBrokerPort` es el puerto hacia los 5 `op` nuevos del broker
(`broker/presentation/dispatcher.py`); su unica implementacion es
`accounts/infrastructure/oauth_broker_client.py`, un socket Unix que
**nunca** puede devolver un token: ninguna forma de este modulo tiene un
campo capaz de llevarlo."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from safent_ads.accounts.domain.oauth_connect_session import OAuthConnectSession
from safent_ads.accounts.domain.platform_account import ApiTier
from safent_ads.accounts.domain.platform_credential import CredentialStatus, PlatformCredential
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.shared.ids import BusinessId, PlatformCode

__all__ = [
    "CredentialRepository",
    "CredentialStatusResult",
    "DiscoveredAccount",
    "OAuthBeginResult",
    "OAuthBrokerPort",
    "OAuthCompleteResult",
    "OAuthConnectSessionRepository",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscoveredAccount:
    """Nunca lleva el token (mismo DTO, sin el, que
    `broker.application.oauth_connect_flow.DiscoveredAccount`)."""

    platform: PlatformCode
    external_account_id: str
    label: str
    currency: str
    timezone: str
    api_tier: ApiTier
    credential_ref_id: CredentialRefId
    scopes: frozenset[str]
    obtained_at: datetime
    expires_at: datetime | None
    business_id: str | None = None
    connection_id: str | None = None
    owner_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class OAuthBeginResult:
    authorization_url: str
    state: str
    expires_at: datetime
    connection_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class OAuthCompleteResult:
    accounts: Sequence[DiscoveredAccount]


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialStatusResult:
    status: CredentialStatus
    scopes: frozenset[str]
    expires_at: datetime | None
    last_validated_at: datetime | None


class OAuthBrokerPort(Protocol):
    async def begin(
        self,
        provider: PlatformCode,
        business_id: BusinessId,
        redirect_uri: str,
        *,
        owner_id: str | None = None,
        google_customer_id: str | None = None,
    ) -> OAuthBeginResult: ...

    async def complete(self, state: str, code: str) -> OAuthCompleteResult: ...

    async def credential_status(
        self, credential_ref_id: CredentialRefId
    ) -> CredentialStatusResult: ...

    async def revoke_credential(self, credential_ref_id: CredentialRefId) -> None: ...

    async def register_meta_system_user_token(
        self,
        token: str,
        *,
        business_id: str | None = None,
        owner_id: str | None = None,
    ) -> OAuthCompleteResult: ...


class CredentialRepository(Protocol):
    """Persistencia de `PlatformCredential` sobre `credential_refs`
    (migracion 0013). La tabla no tiene columna `business_id` -- lo aporta
    el llamante, que ya lo conoce por el `PlatformAccount` propietario."""

    async def get(
        self, credential_ref_id: CredentialRefId, *, business_id: BusinessId
    ) -> PlatformCredential | None: ...

    async def save(self, credential: PlatformCredential) -> None: ...


class OAuthConnectSessionRepository(Protocol):
    """Persistencia de `OAuthConnectSession` sobre `oauth_connect_sessions`
    (migracion 0013). `get_by_id` sirve el sondeo del panel
    (`GET .../connect/{provider}/status?session_id`, contracts/rest-api.md:
    "conocer session_id no autoriza" — el router comprueba ademas
    `session.business_id`); `get_by_state_hash` sirve el callback, que la
    plataforma llama sin cookie de sesion."""

    async def save(self, session: OAuthConnectSession) -> None: ...

    async def get_by_id(self, session_id: uuid.UUID) -> OAuthConnectSession | None: ...

    async def get_by_state_hash(self, state_hash: str) -> OAuthConnectSession | None: ...


class SoleOwnerLookupPort(Protocol):
    """Whom a single-owner installation (`ADS_SINGLE_OWNER_MODE`) belongs to.

    Returns the owner id only when exactly one owner exists; otherwise None so
    the caller fails closed instead of guessing."""

    async def sole_owner_id(self) -> uuid.UUID | None: ...
