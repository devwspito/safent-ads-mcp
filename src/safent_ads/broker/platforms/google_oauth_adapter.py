"""`GoogleOAuthAdapter`: OAuth 2.0 + PKCE contra Google, scopes de Ads y
Tag Manager, acceso offline
(research/ads-platforms-and-mcps.md §3). Tras el canje, descubre las
cuentas accesibles con `customers:listAccessibleCustomers` y su
`currency`/`time_zone` con una consulta GAQL de una sola fila por cuenta —
el mismo patron de "SDK mocked in tests" que `GoogleAdsAdapter`
(`broker/platforms/google_ads_adapter.py`), aqui sobre HTTP en vez de
gRPC porque el intercambio de OAuth no tiene equivalente en
`google-ads-python`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlencode

from safent_ads.broker.platforms.oauth_http import OAuthHttpClient
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError

_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - endpoint, no secreto
_LIST_CUSTOMERS_ENDPOINT = "https://googleads.googleapis.com/v25/customers:listAccessibleCustomers"
_SEARCH_ENDPOINT = "https://googleads.googleapis.com/v25/customers/{customer_id}/googleAds:search"
_SCOPES = (
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
    "https://www.googleapis.com/auth/tagmanager.publish",
)
_SCOPE = " ".join(_SCOPES)
_CUSTOMER_QUERY = (
    "SELECT customer.currency_code, customer.time_zone, customer.descriptive_name "
    "FROM customer LIMIT 1"
)


class GoogleOAuthError(InfrastructureError):
    """Fallo irrecuperable del intercambio OAuth de Google, ya saneado."""


class GoogleOAuthMissingRefreshTokenError(GoogleOAuthError):
    """Google no devolvio `refresh_token`: solo ocurre si `prompt=consent`
    no viaja en `authorization_url` y el propietario ya habia autorizado
    antes. Sin `refresh_token` la cuenta no queda operativa: se rechaza en
    vez de guardar una credencial que caducara con la sesion."""


@dataclass(frozen=True, slots=True)
class GoogleOAuthAdapterConfig:
    client_id: str
    client_secret: str = field(default="", repr=False)
    login_customer_id: str | None = None
    client_type: Literal["web", "desktop"] = "web"

    def __post_init__(self) -> None:
        if (
            self.client_type not in {"web", "desktop"}
            or (self.client_type == "web" and not self.client_secret)
            or (self.client_type == "desktop" and bool(self.client_secret))
        ):
            raise GoogleOAuthError("Configuracion OAuth de Google incompleta")


@dataclass(frozen=True, slots=True)
class GoogleOAuthTokens:
    refresh_token: str = field(repr=False)
    access_token: str = field(repr=False)
    expires_at: datetime
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GoogleCustomerMetadata:
    customer_id: str
    currency: str
    timezone: str
    descriptive_name: str


class GoogleOAuthAdapter:
    def __init__(
        self, config: GoogleOAuthAdapterConfig, http_client: OAuthHttpClient, clock: Clock
    ) -> None:
        self._config = config
        self._http = http_client
        self._clock = clock

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "access_type": "offline",
            # Sin esto Google omite `refresh_token` si el propietario ya
            # concedio el permiso antes (Google OAuth docs).
            "prompt": "consent",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{_AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> GoogleOAuthTokens:
        response = await self._http.post_form(
            _TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._config.client_id,
                **(
                    {"client_secret": self._config.client_secret}
                    if self._config.client_type == "web"
                    else {}
                ),
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        refresh_token = response.get("refresh_token")
        if not refresh_token:
            raise GoogleOAuthMissingRefreshTokenError("Google no devolvio refresh_token")
        expires_in = int(response.get("expires_in", 3600))
        return GoogleOAuthTokens(
            refresh_token=str(refresh_token),
            access_token=str(response["access_token"]),
            expires_at=self._clock.now() + timedelta(seconds=expires_in),
            scopes=tuple(str(response.get("scope", _SCOPE)).split()),
        )

    async def list_accessible_customers(self, access_token: str) -> Sequence[str]:
        response = await self._http.get_json(
            _LIST_CUSTOMERS_ENDPOINT, headers=self._auth_headers(access_token)
        )
        resource_names = response.get("resourceNames", [])
        return [str(name).removeprefix("customers/") for name in resource_names]

    async def fetch_customer_metadata(
        self, customer_id: str, access_token: str
    ) -> GoogleCustomerMetadata:
        url = _SEARCH_ENDPOINT.format(customer_id=customer_id)
        response = await self._http.post_json(
            url,
            json_body={"query": _CUSTOMER_QUERY},
            headers=self._auth_headers(access_token),
        )
        results = response.get("results", [])
        if not results:
            raise GoogleOAuthError(f"la cuenta {customer_id} no devolvio metadatos")
        customer = results[0]["customer"]
        return GoogleCustomerMetadata(
            customer_id=customer_id,
            currency=str(customer["currencyCode"]),
            timezone=str(customer["timeZone"]),
            descriptive_name=str(customer.get("descriptiveName", customer_id)),
        )

    def _auth_headers(self, access_token: str) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {access_token}"}
        if self._config.login_customer_id:
            headers["login-customer-id"] = self._config.login_customer_id
        return headers
