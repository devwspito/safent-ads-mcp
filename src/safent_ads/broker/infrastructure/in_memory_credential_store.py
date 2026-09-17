"""Adaptador de PRODUCCION de esta fase para `CredentialStorePort`
(`broker/application/ports.py`) -- no un doble de test. Sin ninguna cuenta
conectada por defecto: correcto hasta que aterrice `us3-oauth-connect`
(`broker/infrastructure/credential_store.py`, AES-256-GCM sobre
`platform_credentials`). Fail-closed por construccion: nunca inventa una
credencial, `get_credential` devuelve `None` para toda cuenta que no se
haya registrado explicitamente (solo posible hoy inyectando el mapeo en
el constructor, p.ej. desde tests)."""

from __future__ import annotations

from collections.abc import Mapping

from safent_ads.broker.application.ports import PlatformCredential
from safent_ads.shared.ids import PlatformCode

_CredentialKey = tuple[PlatformCode, str]


class InMemoryCredentialStore:
    def __init__(
        self, credentials: Mapping[_CredentialKey, PlatformCredential] | None = None
    ) -> None:
        self._credentials: dict[_CredentialKey, PlatformCredential] = dict(credentials or {})

    async def get_credential(
        self, platform: PlatformCode, external_account_id: str
    ) -> PlatformCredential | None:
        return self._credentials.get((platform, external_account_id))
