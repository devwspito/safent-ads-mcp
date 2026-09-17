"""Casos de uso de `/platform-apps` (owner decision, app-credentials-ui):
el propietario teclea las credenciales de VENDOR (OAuth
de Google, app de Meta) desde el panel. Sin lógica propia más allá de
reenviar al bróker -- la validación de forma/longitud vive en
`accounts/presentation` (REST, el único camino de escritura hasta aquí) y
el enmascarado lo calcula el propio bróker, nunca esta capa."""

from __future__ import annotations

from safent_ads.accounts.application.platform_apps_ports import (
    GoogleAppCredentialsInput,
    MetaAppCredentialsInput,
    PlatformAppsBrokerPort,
    PlatformAppStatus,
)
from safent_ads.shared.ids import PlatformCode


class GetPlatformAppStatus:
    def __init__(self, broker: PlatformAppsBrokerPort) -> None:
        self._broker = broker

    async def execute(self, platform: PlatformCode) -> PlatformAppStatus:
        return await self._broker.get_app_status(platform)


class SetGooglePlatformAppCredentials:
    def __init__(self, broker: PlatformAppsBrokerPort) -> None:
        self._broker = broker

    async def execute(self, credentials: GoogleAppCredentialsInput) -> PlatformAppStatus:
        return await self._broker.set_google_app_credentials(credentials)


class SetMetaPlatformAppCredentials:
    def __init__(self, broker: PlatformAppsBrokerPort) -> None:
        self._broker = broker

    async def execute(self, credentials: MetaAppCredentialsInput) -> PlatformAppStatus:
        return await self._broker.set_meta_app_credentials(credentials)


class DeletePlatformAppCredentials:
    def __init__(self, broker: PlatformAppsBrokerPort) -> None:
        self._broker = broker

    async def execute(self, platform: PlatformCode) -> None:
        await self._broker.delete_app_credentials(platform)
