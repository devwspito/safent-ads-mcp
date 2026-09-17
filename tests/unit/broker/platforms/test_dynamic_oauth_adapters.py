"""`DynamicGoogleOAuthAdapter`/`DynamicMetaOAuthAdapter`: resuelven la app
de VENDOR desde `AppCredentialsStorePort` en cada llamada (owner decision,
app-credentials-ui) -- el almacen gana sobre el respaldo de entorno, y sin
ninguno de los dos la operacion falla cerrado con
`AppCredentialsNotConfiguredError`. La via de pegar un System User token de
Meta nunca exige la app (regla de producto, no es una regresion)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from safent_ads.broker.application.errors import AppCredentialsNotConfiguredError
from safent_ads.broker.application.ports import GoogleAppCredentials, MetaAppCredentials
from safent_ads.broker.platforms.dynamic_oauth_adapters import (
    DynamicGoogleOAuthAdapter,
    DynamicMetaOAuthAdapter,
)
from safent_ads.broker.platforms.google_oauth_adapter import GoogleOAuthAdapterConfig
from safent_ads.broker.platforms.meta_oauth_adapter import MetaOAuthAdapterConfig
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 10, tzinfo=UTC)
_ME_ENDPOINT = "https://graph.facebook.com/v26.0/me"
_AD_ACCOUNTS_ENDPOINT = "https://graph.facebook.com/v26.0/me/adaccounts"


class _FakeStore:
    def __init__(
        self,
        *,
        google: GoogleAppCredentials | None = None,
        meta: MetaAppCredentials | None = None,
    ) -> None:
        self._google = google
        self._meta = meta

    def get_google_app_credentials(self) -> GoogleAppCredentials | None:
        return self._google

    def get_meta_app_credentials(self) -> MetaAppCredentials | None:
        return self._meta


class _UnreachableHttpClient:
    async def post_form(self, url: str, *, data: Mapping[str, str]) -> Mapping[str, Any]:  # noqa: ARG002
        raise AssertionError(f"no deberia llamar a {url}")

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,  # noqa: ARG002
        params: Mapping[str, str] | None = None,  # noqa: ARG002
    ) -> Mapping[str, Any]:
        raise AssertionError(f"no deberia llamar a {url}")

    async def post_json(
        self, url: str, *, json_body: Mapping[str, Any], headers: Mapping[str, str] | None = None  # noqa: ARG002
    ) -> Mapping[str, Any]:
        raise AssertionError(f"no deberia llamar a {url}")


class _FakeMetaHttpClient:
    def __init__(self) -> None:
        self.received_params: list[Mapping[str, str]] = []

    async def post_form(self, url: str, *, data: Mapping[str, str]) -> Mapping[str, Any]:  # noqa: ARG002
        raise AssertionError(f"no deberia llamar a {url}")

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,  # noqa: ARG002
        params: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        self.received_params.append(params or {})
        if url == _ME_ENDPOINT:
            return {"id": "10000000"}
        if url == _AD_ACCOUNTS_ENDPOINT:
            return {"data": []}
        raise AssertionError(f"url inesperada: {url}")

    async def post_json(
        self, url: str, *, json_body: Mapping[str, Any], headers: Mapping[str, str] | None = None  # noqa: ARG002
    ) -> Mapping[str, Any]:
        raise AssertionError(f"no deberia llamar a {url}")


def _google_credentials(**overrides: object) -> GoogleAppCredentials:
    defaults: dict[str, object] = {
        "client_id": "stored-client-id.apps.googleusercontent.com",
        "client_secret": "stored-secret",
        "login_customer_id": "1234567890",
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return GoogleAppCredentials(**defaults)  # type: ignore[arg-type]


def _meta_credentials(**overrides: object) -> MetaAppCredentials:
    defaults: dict[str, object] = {
        "app_id": "stored-app-id",
        "app_secret": "stored-secret",
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return MetaAppCredentials(**defaults)  # type: ignore[arg-type]


def _client_id_from(authorization_url: str) -> str:
    query = parse_qs(urlparse(authorization_url).query)
    return query["client_id"][0]


# --- Google ---
def test_google_uses_stored_credentials_when_present() -> None:
    adapter = DynamicGoogleOAuthAdapter(
        _FakeStore(google=_google_credentials()), _UnreachableHttpClient(), FixedClock(_NOW)
    )

    url = adapter.authorization_url(state="s", code_challenge="c", redirect_uri="https://x/cb")

    assert _client_id_from(url) == "stored-client-id.apps.googleusercontent.com"


def test_google_falls_back_to_env_when_store_is_empty() -> None:
    fallback = GoogleOAuthAdapterConfig(
        client_id="env-client-id.apps.googleusercontent.com",
        client_secret="env-secret",
    )
    adapter = DynamicGoogleOAuthAdapter(
        _FakeStore(google=None), _UnreachableHttpClient(), FixedClock(_NOW), fallback=fallback
    )

    url = adapter.authorization_url(state="s", code_challenge="c", redirect_uri="https://x/cb")

    assert _client_id_from(url) == "env-client-id.apps.googleusercontent.com"


def test_google_store_wins_over_env_fallback() -> None:
    fallback = GoogleOAuthAdapterConfig(
        client_id="env-client-id.apps.googleusercontent.com",
        client_secret="env-secret",
    )
    adapter = DynamicGoogleOAuthAdapter(
        _FakeStore(google=_google_credentials()),
        _UnreachableHttpClient(),
        FixedClock(_NOW),
        fallback=fallback,
    )

    url = adapter.authorization_url(state="s", code_challenge="c", redirect_uri="https://x/cb")

    assert _client_id_from(url) == "stored-client-id.apps.googleusercontent.com"


def test_google_without_store_or_fallback_fails_closed() -> None:
    adapter = DynamicGoogleOAuthAdapter(
        _FakeStore(google=None), _UnreachableHttpClient(), FixedClock(_NOW)
    )

    with pytest.raises(AppCredentialsNotConfiguredError):
        adapter.authorization_url(state="s", code_challenge="c", redirect_uri="https://x/cb")


async def test_google_exchange_code_without_credentials_fails_closed() -> None:
    adapter = DynamicGoogleOAuthAdapter(
        _FakeStore(google=None), _UnreachableHttpClient(), FixedClock(_NOW)
    )

    with pytest.raises(AppCredentialsNotConfiguredError):
        await adapter.exchange_code(code="c", code_verifier="v", redirect_uri="https://x/cb")


# --- Meta ---
def test_meta_uses_stored_credentials_when_present() -> None:
    adapter = DynamicMetaOAuthAdapter(
        _FakeStore(meta=_meta_credentials()), _UnreachableHttpClient(), FixedClock(_NOW)
    )

    url = adapter.authorization_url(state="s", redirect_uri="https://x/cb")

    assert _client_id_from(url) == "stored-app-id"


def test_meta_without_store_or_fallback_fails_closed_on_authorization_url() -> None:
    adapter = DynamicMetaOAuthAdapter(
        _FakeStore(meta=None), _UnreachableHttpClient(), FixedClock(_NOW)
    )

    with pytest.raises(AppCredentialsNotConfiguredError):
        adapter.authorization_url(state="s", redirect_uri="https://x/cb")


async def test_meta_exchange_code_without_credentials_fails_closed() -> None:
    adapter = DynamicMetaOAuthAdapter(
        _FakeStore(meta=None), _UnreachableHttpClient(), FixedClock(_NOW)
    )

    with pytest.raises(AppCredentialsNotConfiguredError):
        await adapter.exchange_code_for_long_lived_token(code="c", redirect_uri="https://x/cb")


async def test_meta_pasted_system_user_token_never_needs_the_app() -> None:
    """Regla de producto: `validate_system_user_token`/`list_ad_accounts`
    no usan `app_id`/`app_secret` -- exigirlos aqui seria una regresion
    frente al comportamiento anterior (`_secret()` con cadena vacia)."""
    adapter = DynamicMetaOAuthAdapter(
        _FakeStore(meta=None), _FakeMetaHttpClient(), FixedClock(_NOW)
    )

    accounts = await adapter.validate_system_user_token("pasted-token")

    assert accounts == []


async def test_meta_list_ad_accounts_never_needs_the_app() -> None:
    adapter = DynamicMetaOAuthAdapter(
        _FakeStore(meta=None), _FakeMetaHttpClient(), FixedClock(_NOW)
    )

    accounts = await adapter.list_ad_accounts("some-token")

    assert accounts == []


def test_meta_store_wins_over_env_fallback() -> None:
    fallback = MetaOAuthAdapterConfig(app_id="env-app-id", app_secret="env-secret")
    adapter = DynamicMetaOAuthAdapter(
        _FakeStore(meta=_meta_credentials()),
        _UnreachableHttpClient(),
        FixedClock(_NOW),
        fallback=fallback,
    )

    url = adapter.authorization_url(state="s", redirect_uri="https://x/cb")

    assert _client_id_from(url) == "stored-app-id"
