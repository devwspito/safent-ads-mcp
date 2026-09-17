"""`MetaOAuthAdapter`: HTTP sustituido por un doble, incluida la via de
pegar un System User token propio."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from safent_ads.broker.platforms.meta_oauth_adapter import (
    MetaOAuthAdapter,
    MetaOAuthAdapterConfig,
    MetaOAuthError,
    MetaOAuthInvalidTokenError,
)
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 9, tzinfo=UTC)
_TOKEN_URL = "https://graph.facebook.com/v26.0/oauth/access_token"  # noqa: S105
_AD_ACCOUNTS_URL = "https://graph.facebook.com/v26.0/me/adaccounts"
_ME_URL = "https://graph.facebook.com/v26.0/me"


class _FakeHttpClient:
    def __init__(
        self,
        *,
        get_json_responses: list[Mapping[str, Any]] | None = None,
        raise_on_me: bool = False,
    ) -> None:
        self._responses = list(get_json_responses or [])
        self._raise_on_me = raise_on_me
        self.received_urls: list[str] = []
        self.received_headers: list[Mapping[str, str]] = []

    async def post_form(self, url: str, *, data: Mapping[str, str]) -> Mapping[str, Any]:  # noqa: ARG002
        raise AssertionError("MetaOAuthAdapter no deberia usar post_form")

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,  # noqa: ARG002
    ) -> Mapping[str, Any]:
        self.received_urls.append(url)
        self.received_headers.append(headers or {})
        if url == _ME_URL and self._raise_on_me:
            raise MetaOAuthInvalidTokenError("token rechazado por Meta")
        return self._responses.pop(0)

    async def post_json(
        self,
        url: str,  # noqa: ARG002
        *,
        json_body: Mapping[str, Any],  # noqa: ARG002
        headers: Mapping[str, str] | None = None,  # noqa: ARG002
    ) -> Mapping[str, Any]:
        raise AssertionError("MetaOAuthAdapter no deberia usar post_json")


def _config() -> MetaOAuthAdapterConfig:
    return MetaOAuthAdapterConfig(app_id="app-id", app_secret="app-secret")


def test_authorization_url_carries_scopes() -> None:
    adapter = MetaOAuthAdapter(_config(), _FakeHttpClient(), FixedClock(_NOW))

    url = adapter.authorization_url(state="the-state", redirect_uri="https://x/callback")

    assert "state=the-state" in url
    assert "ads_read" in url
    assert "ads_management" in url
    assert "business_management" in url


async def test_exchange_code_returns_long_lived_token() -> None:
    http = _FakeHttpClient(
        get_json_responses=[
            {"access_token": "short-lived"},
            {"access_token": "long-lived-token", "expires_in": 5184000},
        ]
    )
    adapter = MetaOAuthAdapter(_config(), http, FixedClock(_NOW))

    tokens = await adapter.exchange_code_for_long_lived_token(
        code="auth-code", redirect_uri="https://x/callback"
    )

    assert tokens.access_token == "long-lived-token"  # noqa: S105
    assert tokens.expires_at is not None
    assert tokens.expires_at > _NOW
    assert http.received_urls == [_TOKEN_URL, _TOKEN_URL]


async def test_list_ad_accounts_maps_rows() -> None:
    http = _FakeHttpClient(
        get_json_responses=[
            {
                "data": [
                    {
                        "account_id": "123",
                        "name": "Cuenta de prueba",
                        "currency": "EUR",
                        "timezone_name": "Europe/Madrid",
                    }
                ]
            }
        ]
    )
    adapter = MetaOAuthAdapter(_config(), http, FixedClock(_NOW))

    accounts = await adapter.list_ad_accounts("access-token")

    assert len(accounts) == 1
    assert accounts[0].account_id == "act_123"
    assert accounts[0].currency == "EUR"
    assert http.received_headers == [{"Authorization": "Bearer access-token"}]


async def test_validate_system_user_token_calls_me_then_adaccounts() -> None:
    http = _FakeHttpClient(
        get_json_responses=[
            {"id": "10000000"},
            {"data": []},
        ]
    )
    adapter = MetaOAuthAdapter(_config(), http, FixedClock(_NOW))

    accounts = await adapter.validate_system_user_token("system-user-token")

    assert accounts == []
    assert http.received_urls == [_ME_URL, _AD_ACCOUNTS_URL]
    assert http.received_headers == [
        {"Authorization": "Bearer system-user-token"},
        {"Authorization": "Bearer system-user-token"},
    ]


async def test_validate_system_user_token_rejects_invalid_token() -> None:
    http = _FakeHttpClient(raise_on_me=True)
    adapter = MetaOAuthAdapter(_config(), http, FixedClock(_NOW))

    with pytest.raises(MetaOAuthInvalidTokenError):
        await adapter.validate_system_user_token("bad-token")


async def test_account_discovery_follows_cursors_without_following_next_urls() -> None:
    row = {"account_id": "123", "currency": "EUR", "timezone_name": "Europe/Madrid"}
    http = _FakeHttpClient(
        get_json_responses=[
            {
                "data": [row],
                "paging": {"next": "https://untrusted.invalid/", "cursors": {"after": "next"}},
            },
            {"data": [{**row, "account_id": "456"}]},
        ]
    )
    accounts = await MetaOAuthAdapter(_config(), http, FixedClock(_NOW)).list_ad_accounts("test")
    assert [account.account_id for account in accounts] == ["act_123", "act_456"]
    assert http.received_urls == [_AD_ACCOUNTS_URL, _AD_ACCOUNTS_URL]


async def test_account_discovery_rejects_repeated_cursor() -> None:
    page = {"data": [], "paging": {"next": "next", "cursors": {"after": "same"}}}
    http = _FakeHttpClient(get_json_responses=[page, page])
    with pytest.raises(MetaOAuthError, match="cursor"):
        await MetaOAuthAdapter(_config(), http, FixedClock(_NOW)).list_ad_accounts("test")
