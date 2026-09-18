"""`GoogleOAuthAdapter`: HTTP sustituido por un doble (mismo patron que
`GoogleAdsAdapter`/`GoogleAdsSearchClient`: "SDK mocked in tests")."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from safent_ads.broker.platforms.google_oauth_adapter import (
    GoogleOAuthAdapter,
    GoogleOAuthAdapterConfig,
    GoogleOAuthError,
    GoogleOAuthMissingRefreshTokenError,
)
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 9, tzinfo=UTC)


class _FakeHttpClient:
    def __init__(
        self,
        *,
        form_responses: dict[str, Mapping[str, Any]] | None = None,
        json_responses: dict[str, Mapping[str, Any]] | None = None,
        post_json_responses: dict[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._form_responses = form_responses or {}
        self._json_responses = json_responses or {}
        self._post_json_responses = post_json_responses or {}
        self.received_headers: list[Mapping[str, str]] = []
        self.received_forms: list[Mapping[str, str]] = []

    async def post_form(self, url: str, *, data: Mapping[str, str]) -> Mapping[str, Any]:  # noqa: ARG002
        self.received_forms.append(dict(data))
        return self._form_responses[url]

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,  # noqa: ARG002
    ) -> Mapping[str, Any]:
        self.received_headers.append(headers or {})
        return self._json_responses[url]

    async def post_json(
        self,
        url: str,
        *,
        json_body: Mapping[str, Any],  # noqa: ARG002
        headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        self.received_headers.append(headers or {})
        return self._post_json_responses[url]


def _config(**overrides: object) -> GoogleOAuthAdapterConfig:
    defaults: dict[str, object] = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "login_customer_id": "1112223333",
    }
    defaults.update(overrides)
    return GoogleOAuthAdapterConfig(**defaults)  # type: ignore[arg-type]


def test_authorization_url_carries_pkce_and_offline_access() -> None:
    adapter = GoogleOAuthAdapter(_config(), _FakeHttpClient(), FixedClock(_NOW))

    url = adapter.authorization_url(
        state="the-state", code_challenge="the-challenge", redirect_uri="https://x/callback"
    )

    assert "state=the-state" in url
    assert "code_challenge=the-challenge" in url
    assert "code_challenge_method=S256" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    scopes = set(parse_qs(urlparse(url).query)["scope"][0].split())
    assert "https://www.googleapis.com/auth/adwords" in scopes
    assert "https://www.googleapis.com/auth/tagmanager.readonly" in scopes
    assert "https://www.googleapis.com/auth/tagmanager.edit.containers" in scopes
    assert "https://www.googleapis.com/auth/tagmanager.edit.containerversions" in scopes
    assert "https://www.googleapis.com/auth/tagmanager.publish" in scopes


async def test_exchange_code_returns_tokens() -> None:
    http = _FakeHttpClient(
        form_responses={
            "https://oauth2.googleapis.com/token": {
                "refresh_token": "refresh-token-value",
                "access_token": "access-token-value",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/adwords",
            }
        }
    )
    adapter = GoogleOAuthAdapter(_config(), http, FixedClock(_NOW))

    tokens = await adapter.exchange_code(
        code="auth-code", code_verifier="verifier", redirect_uri="https://x/callback"
    )

    assert tokens.refresh_token == "refresh-token-value"  # noqa: S105
    assert tokens.access_token == "access-token-value"  # noqa: S105
    assert tokens.expires_at > _NOW


async def test_exchange_code_without_refresh_token_raises() -> None:
    http = _FakeHttpClient(
        form_responses={
            "https://oauth2.googleapis.com/token": {"access_token": "a", "expires_in": 3600}
        }
    )
    adapter = GoogleOAuthAdapter(_config(), http, FixedClock(_NOW))

    with pytest.raises(GoogleOAuthMissingRefreshTokenError):
        await adapter.exchange_code(
            code="auth-code", code_verifier="verifier", redirect_uri="https://x/callback"
        )


async def test_list_accessible_customers_strips_resource_prefix() -> None:
    http = _FakeHttpClient(
        json_responses={
            "https://googleads.googleapis.com/v25/customers:listAccessibleCustomers": {
                "resourceNames": ["customers/1234567890", "customers/9998887777"]
            }
        }
    )
    adapter = GoogleOAuthAdapter(_config(), http, FixedClock(_NOW))

    customer_ids = await adapter.list_accessible_customers("access-token")

    assert customer_ids == ["1234567890", "9998887777"]
    assert http.received_headers[0]["Authorization"] == "Bearer access-token"
    assert http.received_headers[0]["login-customer-id"] == "1112223333"
    assert http.received_headers[0]["login-customer-id"] == "1112223333"


async def test_fetch_customer_metadata_parses_gaql_result() -> None:
    url = "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:search"
    http = _FakeHttpClient(
        post_json_responses={
            url: {
                "results": [
                    {
                        "customer": {
                            "currencyCode": "EUR",
                            "timeZone": "Europe/Madrid",
                            "descriptiveName": "Cliente de prueba",
                        }
                    }
                ]
            }
        }
    )
    adapter = GoogleOAuthAdapter(_config(), http, FixedClock(_NOW))

    metadata = await adapter.fetch_customer_metadata("1234567890", "access-token")

    assert metadata.currency == "EUR"
    assert metadata.timezone == "Europe/Madrid"
    assert metadata.descriptive_name == "Cliente de prueba"


async def test_fetch_customer_metadata_without_results_raises() -> None:
    url = "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:search"
    http = _FakeHttpClient(post_json_responses={url: {"results": []}})
    adapter = GoogleOAuthAdapter(_config(), http, FixedClock(_NOW))

    with pytest.raises(GoogleOAuthError):
        await adapter.fetch_customer_metadata("1234567890", "access-token")


@pytest.mark.parametrize("client_type, secret", [("desktop", ""), ("web", "web-secret")])
async def test_client_kind_controls_token_exchange_secret(client_type: str, secret: str) -> None:
    http = _FakeHttpClient(
        form_responses={
            "https://oauth2.googleapis.com/token": {
                "refresh_token": "refresh-fixture",
                "access_token": "access-fixture",
            }
        }
    )
    adapter = GoogleOAuthAdapter(
        _config(client_type=client_type, client_secret=secret), http, FixedClock(_NOW)
    )
    callback = "http://127.0.0.1:48371/ads/api/v1/platform-accounts/google/reconnect/callback"
    url = adapter.authorization_url(
        state="state", code_challenge="challenge", redirect_uri=callback
    )
    assert "code_challenge_method=S256" in url
    assert "client_secret" not in url
    await adapter.exchange_code(code="code", code_verifier="verifier", redirect_uri=callback)
    data = http.received_forms[0]
    assert data["redirect_uri"] == callback
    assert data["code_verifier"] == "verifier"
    if client_type == "desktop":
        assert "client_secret" not in data
    else:
        assert data["client_secret"] == secret


@pytest.mark.parametrize(
    "client_type, secret", [("web", ""), ("desktop", "not-permitted"), ("invalid", "")]
)
def test_invalid_client_kind_configuration_fails_closed(client_type: str, secret: str) -> None:
    with pytest.raises(GoogleOAuthError):
        _config(client_type=client_type, client_secret=secret)
