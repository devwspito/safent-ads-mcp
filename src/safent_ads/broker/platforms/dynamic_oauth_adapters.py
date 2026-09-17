"""`DynamicGoogleOAuthAdapter`/`DynamicMetaOAuthAdapter`: envuelven
`GoogleOAuthAdapter`/`MetaOAuthAdapter` resolviendo las credenciales de
VENDOR desde `AppCredentialsStorePort` en cada llamada, en vez de una vez
al arrancar `ads-broker` -- mismo patron que
`broker/platforms/live_google_ads_client.py::_resolve_credential` ya usa
para la credencial de CLIENTE. Asi el propietario puede teclear o rotar la
app de Google/Meta desde el panel sin reiniciar el broker (owner decision,
app-credentials-ui).

`fallback` (`BrokerSettings`/entorno) es solo para desarrollo: si el
almacen no tiene nada guardado, se usa en su lugar; si tampoco hay
`fallback`, la operacion falla cerrado con `AppCredentialsNotConfiguredError`
-- `broker/presentation/dispatcher.py` la traduce a
`PLATFORM_APP_NOT_CONFIGURED` (contracts/rest-api.md, 409 en
`POST .../reconnect/start`).

Meta tiene una via que NUNCA necesita `app_id`/`app_secret`: pegar un
System User token propio (`validate_system_user_token`/`list_ad_accounts`,
ninguno de los dos toca `MetaOAuthAdapterConfig`). Exigir la app ahi seria
una regresion -- solo `authorization_url`/`exchange_code_for_long_lived_token`
(el OAuth real) fallan cerrado sin ella."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from safent_ads.broker.application.errors import AppCredentialsNotConfiguredError
from safent_ads.broker.application.ports import AppCredentialsStorePort, GoogleAppCredentials
from safent_ads.broker.platforms.google_oauth_adapter import (
    GoogleCustomerMetadata,
    GoogleOAuthAdapter,
    GoogleOAuthAdapterConfig,
    GoogleOAuthTokens,
)
from safent_ads.broker.platforms.meta_oauth_adapter import (
    MetaAdAccount,
    MetaOAuthAdapter,
    MetaOAuthAdapterConfig,
    MetaOAuthTokens,
)
from safent_ads.broker.platforms.oauth_http import OAuthHttpClient
from safent_ads.shared.clock import Clock

_EMPTY_META_CONFIG = MetaOAuthAdapterConfig(app_id="", app_secret="")


class DynamicGoogleOAuthAdapter:
    def __init__(
        self,
        store: AppCredentialsStorePort,
        http_client: OAuthHttpClient,
        clock: Clock,
        *,
        fallback: GoogleOAuthAdapterConfig | None = None,
    ) -> None:
        self._store = store
        self._http = http_client
        self._clock = clock
        self._fallback = fallback

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        return self._required().authorization_url(
            state=state, code_challenge=code_challenge, redirect_uri=redirect_uri
        )

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> GoogleOAuthTokens:
        return await self._required().exchange_code(
            code=code, code_verifier=code_verifier, redirect_uri=redirect_uri
        )

    async def list_accessible_customers(self, access_token: str) -> Sequence[str]:
        return await self._required().list_accessible_customers(access_token)

    async def fetch_customer_metadata(
        self, customer_id: str, access_token: str
    ) -> GoogleCustomerMetadata:
        return await self._required().fetch_customer_metadata(customer_id, access_token)

    def _required(self) -> GoogleOAuthAdapter:
        stored = self._store.get_google_app_credentials()
        config = _to_google_config(stored) if stored is not None else self._fallback
        if config is None:
            raise AppCredentialsNotConfiguredError("google")
        return GoogleOAuthAdapter(config, self._http, self._clock)


class DynamicMetaOAuthAdapter:
    def __init__(
        self,
        store: AppCredentialsStorePort,
        http_client: OAuthHttpClient,
        clock: Clock,
        *,
        fallback: MetaOAuthAdapterConfig | None = None,
        request_mcp_access: bool = False,
    ) -> None:
        self._store = store
        self._http = http_client
        self._clock = clock
        self._fallback = fallback
        self._request_mcp_access = request_mcp_access

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return self._required().authorization_url(state=state, redirect_uri=redirect_uri)

    async def exchange_code_for_long_lived_token(
        self, *, code: str, redirect_uri: str
    ) -> MetaOAuthTokens:
        return await self._required().exchange_code_for_long_lived_token(
            code=code, redirect_uri=redirect_uri
        )

    async def list_ad_accounts(self, access_token: str) -> Sequence[MetaAdAccount]:
        return await self._optional().list_ad_accounts(access_token)

    async def validate_system_user_token(self, token: str) -> Sequence[MetaAdAccount]:
        return await self._optional().validate_system_user_token(token)

    def _required(self) -> MetaOAuthAdapter:
        config = self._resolve_config()
        if config is None:
            raise AppCredentialsNotConfiguredError("meta")
        return MetaOAuthAdapter(config, self._http, self._clock)

    def _optional(self) -> MetaOAuthAdapter:
        config = self._resolve_config() or _EMPTY_META_CONFIG
        return MetaOAuthAdapter(config, self._http, self._clock)

    def _resolve_config(self) -> MetaOAuthAdapterConfig | None:
        stored = self._store.get_meta_app_credentials()
        if stored is not None:
            return MetaOAuthAdapterConfig(
                app_id=stored.app_id,
                app_secret=stored.app_secret,
                request_mcp_access=self._request_mcp_access,
            )
        return (
            replace(self._fallback, request_mcp_access=self._request_mcp_access)
            if self._fallback is not None
            else None
        )


def _to_google_config(stored: GoogleAppCredentials) -> GoogleOAuthAdapterConfig:
    return GoogleOAuthAdapterConfig(
        client_id=stored.client_id,
        client_type=stored.client_type,
        client_secret=stored.client_secret,
        login_customer_id=stored.login_customer_id,
    )
