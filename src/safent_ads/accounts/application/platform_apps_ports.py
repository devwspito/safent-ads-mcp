"""Puerto hacia los 3 `op` nuevos de credenciales de VENDOR del bróker
(owner decision, app-credentials-ui): `set_platform_app_credentials`/
`get_platform_app_status`/`delete_platform_app_credentials`.
`accounts/infrastructure/oauth_broker_client.py::OAuthBrokerSocketClient`
es su única implementación de producción, sobre el mismo socket que
`OAuthBrokerPort` (`connect_ports.py`) -- ninguna forma de este módulo
tiene un campo capaz de llevar el secreto de vuelta, solo el estado ya
enmascarado que calcula el bróker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from safent_ads.shared.ids import PlatformCode

__all__ = [
    "GoogleAppCredentialsInput",
    "MetaAppCredentialsInput",
    "PlatformAppStatus",
    "PlatformAppsBrokerPort",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class GoogleAppCredentialsInput:
    client_id: str
    client_secret: str = ""
    login_customer_id: str | None
    client_type: Literal["web", "desktop"] = "web"


@dataclass(frozen=True, slots=True, kw_only=True)
class MetaAppCredentialsInput:
    app_id: str
    app_secret: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformAppStatus:
    platform: PlatformCode
    configured: bool
    client_id_masked: str | None
    login_customer_id_masked: str | None
    updated_at: datetime | None
    client_type: Literal["web", "desktop"] | None = None


class PlatformAppsBrokerPort(Protocol):
    async def set_google_app_credentials(
        self, credentials: GoogleAppCredentialsInput
    ) -> PlatformAppStatus: ...

    async def set_meta_app_credentials(
        self, credentials: MetaAppCredentialsInput
    ) -> PlatformAppStatus: ...

    async def get_app_status(self, platform: PlatformCode) -> PlatformAppStatus: ...

    async def delete_app_credentials(self, platform: PlatformCode) -> None: ...
