"""`MetaOAuthAdapter`: Facebook Login for Business sobre Graph API v26
(research/ads-platforms-and-mcps.md §3), scopes `ads_read,ads_management,
business_management`. Tras el canje, cambia el token corto por uno de
larga duracion y descubre `me/adaccounts`.

Tambien valida la via alternativa de pegar un System User token propio
(`validate_system_user_token`): llama `me` para confirmar que el token es
valido y luego `me/adaccounts`, exactamente igual que el resultado de un
OAuth completo — el llamante no distingue el origen del token una vez
guardado."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlencode

from safent_ads.broker.platforms.oauth_http import OAuthHttpClient
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError

_AUTHORIZATION_ENDPOINT = "https://www.facebook.com/v26.0/dialog/oauth"
_TOKEN_ENDPOINT = "https://graph.facebook.com/v26.0/oauth/access_token"  # noqa: S105
_AD_ACCOUNTS_ENDPOINT = "https://graph.facebook.com/v26.0/me/adaccounts"
_ME_ENDPOINT = "https://graph.facebook.com/v26.0/me"
_SCOPES = ("ads_read", "ads_management", "business_management")
_AD_ACCOUNT_FIELDS = "account_id,name,currency,timezone_name"
_META_ACCOUNT_PREFIX = "act_"


class MetaOAuthError(InfrastructureError):
    """Fallo irrecuperable del intercambio OAuth de Meta, ya saneado."""


class MetaOAuthInvalidTokenError(MetaOAuthError):
    """El token (OAuth o System User pegado) no supero `me`: la
    plataforma lo rechaza, nunca se guarda (contracts §"Conexiones")."""


@dataclass(frozen=True, slots=True)
class MetaOAuthAdapterConfig:
    app_id: str
    app_secret: str = field(repr=False)
    request_mcp_access: bool = False


@dataclass(frozen=True, slots=True)
class MetaOAuthTokens:
    access_token: str = field(repr=False)
    expires_at: datetime | None  # No expiry reported; revocation is still possible.


@dataclass(frozen=True, slots=True)
class MetaAdAccount:
    account_id: str
    name: str
    currency: str
    timezone_name: str


class MetaOAuthAdapter:
    def __init__(
        self, config: MetaOAuthAdapterConfig, http_client: OAuthHttpClient, clock: Clock
    ) -> None:
        self._config = config
        self._http = http_client
        self._clock = clock

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._config.app_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "scope": ",".join(
                _SCOPES + (("ads_mcp_management",) if self._config.request_mcp_access else ())
            ),
        }
        return f"{_AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    async def exchange_code_for_long_lived_token(
        self, *, code: str, redirect_uri: str
    ) -> MetaOAuthTokens:
        short_lived = await self._http.get_json(
            _TOKEN_ENDPOINT,
            params={
                "client_id": self._config.app_id,
                "client_secret": self._config.app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
        )
        return await self._exchange_for_long_lived(str(short_lived["access_token"]))

    async def _exchange_for_long_lived(self, short_lived_token: str) -> MetaOAuthTokens:
        response = await self._http.get_json(
            _TOKEN_ENDPOINT,
            params={
                "grant_type": "fb_exchange_token",
                "client_id": self._config.app_id,
                "client_secret": self._config.app_secret,
                "fb_exchange_token": short_lived_token,
            },
        )
        expires_in = response.get("expires_in")
        expires_at = self._clock.now() + timedelta(seconds=int(expires_in)) if expires_in else None
        return MetaOAuthTokens(access_token=str(response["access_token"]), expires_at=expires_at)

    async def list_ad_accounts(self, access_token: str) -> Sequence[MetaAdAccount]:
        accounts: list[MetaAdAccount] = []
        params = {"fields": _AD_ACCOUNT_FIELDS}
        seen: set[str] = set()
        while True:
            response = await self._http.get_json(
                _AD_ACCOUNTS_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
            )
            data = response.get("data")
            if not isinstance(data, list):
                raise MetaOAuthError("respuesta Meta sin lista de cuentas")
            accounts.extend(_to_ad_account(row) for row in data)
            paging = response.get("paging", {})
            if not paging.get("next"):
                return accounts
            cursor = paging.get("cursors", {}).get("after")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise MetaOAuthError("cursor Meta ausente o repetido")
            seen.add(cursor)
            params = {**params, "after": cursor}

    async def validate_system_user_token(self, token: str) -> Sequence[MetaAdAccount]:
        try:
            await self._http.get_json(_ME_ENDPOINT, headers={"Authorization": f"Bearer {token}"})
        except Exception as exc:  # noqa: BLE001 - frontera con el proveedor, nunca fail-open
            raise MetaOAuthInvalidTokenError("token rechazado por Meta") from exc
        return await self.list_ad_accounts(token)


def _to_ad_account(row: dict[str, object]) -> MetaAdAccount:
    raw_account_id = str(row["account_id"])
    return MetaAdAccount(
        account_id=(
            raw_account_id
            if raw_account_id.startswith(_META_ACCOUNT_PREFIX)
            else f"{_META_ACCOUNT_PREFIX}{raw_account_id}"
        ),
        name=str(row.get("name", row["account_id"])),
        currency=str(row["currency"]),
        timezone_name=str(row["timezone_name"]),
    )
