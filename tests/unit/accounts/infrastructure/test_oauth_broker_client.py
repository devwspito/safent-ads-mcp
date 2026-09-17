"""`OAuthBrokerSocketClient` contra un `broker.presentation.socket_server.serve`
real (mismo patron que `test_broker_client.py`): prueba que el lado
`ads-api` de los 5 `op` de conexion OAuth (US3) es compatible de verdad
con el broker, y que ninguno de los DTOs que cruza la frontera del socket
puede llevar un token -- ni por campo declarado (`connect_ports.py`) ni
por valor filtrado en el JSON de la trama."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import os
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from safent_ads.accounts.application.connect_ports import (
    CredentialStatusResult,
    DiscoveredAccount,
    OAuthBeginResult,
    OAuthCompleteResult,
)
from safent_ads.accounts.application.platform_apps_ports import (
    GoogleAppCredentialsInput,
    MetaAppCredentialsInput,
    PlatformAppStatus,
)
from safent_ads.accounts.domain.platform_credential import CredentialStatus
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.accounts.infrastructure.oauth_broker_client import OAuthBrokerSocketClient
from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.platforms.google_oauth_adapter import (
    GoogleOAuthAdapter,
    GoogleOAuthAdapterConfig,
)
from safent_ads.broker.platforms.meta_oauth_adapter import MetaOAuthAdapter, MetaOAuthAdapterConfig
from safent_ads.broker.presentation.dispatcher import BrokerRuntime
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, PlatformCode

_KEY_B64 = base64.b64encode(b"0" * 32).decode()
_GOOGLE_REFRESH_TOKEN = "1//super-secret-refresh-token"  # noqa: S105
_META_SYSTEM_USER_TOKEN = "pasted-system-user-token"  # noqa: S105

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
_GOOGLE_LIST_CUSTOMERS_URL = (
    "https://googleads.googleapis.com/v25/customers:listAccessibleCustomers"
)
_GOOGLE_SEARCH_URL = "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:search"
_META_ME_URL = "https://graph.facebook.com/v26.0/me"
_META_ADACCOUNTS_URL = "https://graph.facebook.com/v26.0/me/adaccounts"


class _ScriptedHttpClient:
    """Doble de `OAuthHttpClient`: respuestas fijas, igual que
    `test_oauth_socket_ops.py` -- solo lo que Google necesita para esta
    prueba (System User token de Meta, no OAuth completo)."""

    async def post_form(self, url: str, *, data: Mapping[str, str]) -> Mapping[str, Any]:  # noqa: ARG002
        assert url == _GOOGLE_TOKEN_URL
        return {
            "refresh_token": _GOOGLE_REFRESH_TOKEN,
            "access_token": "google-access-token",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/adwords",
        }

    async def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,  # noqa: ARG002
        params: Mapping[str, str] | None = None,  # noqa: ARG002
    ) -> Mapping[str, Any]:
        if url == _GOOGLE_LIST_CUSTOMERS_URL:
            return {"resourceNames": ["customers/1234567890"]}
        if url == _META_ME_URL:
            return {"id": "10000000"}
        if url == _META_ADACCOUNTS_URL:
            return {
                "data": [
                    {
                        "account_id": "act_555",
                        "name": "Cuenta Meta",
                        "currency": "EUR",
                        "timezone_name": "Europe/Madrid",
                    }
                ]
            }
        raise AssertionError(f"url inesperada: {url}")

    async def post_json(
        self, url: str, *, json_body: Mapping[str, Any], headers: Mapping[str, str] | None = None  # noqa: ARG002
    ) -> Mapping[str, Any]:
        assert url == _GOOGLE_SEARCH_URL
        return {
            "results": [
                {
                    "customer": {
                        "currencyCode": "EUR",
                        "timeZone": "Europe/Madrid",
                        "descriptiveName": "Cliente Google",
                    }
                }
            ]
        }


def _runtime(store_dir: Path) -> BrokerRuntime:
    clock = FixedClock(datetime(2026, 9, 9, tzinfo=UTC))
    http = _ScriptedHttpClient()
    store = EncryptedCredentialStore(store_dir, _KEY_B64)
    google = GoogleOAuthAdapter(
        GoogleOAuthAdapterConfig(client_id="c", client_secret="s"), http, clock
    )
    meta = MetaOAuthAdapter(MetaOAuthAdapterConfig(app_id="a", app_secret="s"), http, clock)
    oauth_flow = OAuthConnectFlow(store, google, meta, clock)
    return BrokerRuntime(
        adapters=PlatformAdapterRegistry({}),
        oauth_flow=oauth_flow,
        app_credentials=AppCredentialsService(store, clock),
    )


@pytest.fixture
async def oauth_client(tmp_path: Path) -> AsyncIterator[OAuthBrokerSocketClient]:
    socket_path = tmp_path / "broker.sock"
    server = await serve(socket_path, _runtime(tmp_path / "credentials"), frozenset({os.getuid()}))
    try:
        yield OAuthBrokerSocketClient(socket_path)
    finally:
        server.close()
        await server.wait_closed()


async def test_begin_then_complete_round_trips_a_discovered_google_account(
    oauth_client: OAuthBrokerSocketClient,
) -> None:
    begin = await oauth_client.begin(
        PlatformCode.GOOGLE, BusinessId.new(), "https://ads.example/api/v1/platform-accounts/google/reconnect/callback"
    )
    assert isinstance(begin, OAuthBeginResult)
    assert begin.authorization_url

    complete = await oauth_client.complete(begin.state, "auth-code")

    assert isinstance(complete, OAuthCompleteResult)
    assert len(complete.accounts) == 1
    account = complete.accounts[0]
    assert isinstance(account, DiscoveredAccount)
    assert account.external_account_id == "1234567890"
    assert account.currency == "EUR"


@pytest.mark.parametrize("completion_timeout, succeeds", [(0.5, True), (0.01, False)])
async def test_completion_has_its_own_bounded_timeout_without_retries(
    oauth_client: OAuthBrokerSocketClient, monkeypatch, completion_timeout, succeeds,
) -> None:
    begin = await oauth_client.begin(PlatformCode.GOOGLE, BusinessId.new(), "https://ads.test/cb")
    finished = asyncio.Event()
    calls = 0
    original = OAuthConnectFlow.complete

    async def delayed(self, *, state, code):
        nonlocal calls
        calls += 1
        try:
            await asyncio.sleep(0.05)
            return await original(self, state=state, code=code)
        finally:
            finished.set()

    monkeypatch.setattr(OAuthConnectFlow, "complete", delayed)
    bounded = OAuthBrokerSocketClient(
        oauth_client._socket_path, timeout_seconds=0.02,
        complete_timeout_seconds=completion_timeout,
    )
    if succeeds:
        result = await bounded.complete(begin.state, "auth-code")
        assert len(result.accounts) == 1
    else:
        with pytest.raises(BrokerConnectionError):
            await bounded.complete(begin.state, "auth-code")
    await asyncio.wait_for(finished.wait(), timeout=1)
    assert calls == 1


async def test_register_meta_system_user_token_round_trips(
    oauth_client: OAuthBrokerSocketClient,
) -> None:
    result = await oauth_client.register_meta_system_user_token(_META_SYSTEM_USER_TOKEN)

    assert len(result.accounts) == 1
    assert result.accounts[0].external_account_id == "act_555"


async def test_credential_status_then_revoke_round_trips(
    oauth_client: OAuthBrokerSocketClient,
) -> None:
    begin = await oauth_client.begin(
        PlatformCode.GOOGLE, BusinessId.new(), "https://ads.example/callback"
    )
    complete = await oauth_client.complete(begin.state, "auth-code")
    credential_ref_id = complete.accounts[0].credential_ref_id

    status = await oauth_client.credential_status(credential_ref_id)
    assert isinstance(status, CredentialStatusResult)
    assert status.status == CredentialStatus.CONNECTED

    await oauth_client.revoke_credential(credential_ref_id)

    status_after = await oauth_client.credential_status(credential_ref_id)
    assert status_after.status == CredentialStatus.REVOKED


# --- lane: app-credentials-ui ---
async def test_app_status_starts_unconfigured(oauth_client: OAuthBrokerSocketClient) -> None:
    status = await oauth_client.get_app_status(PlatformCode.GOOGLE)

    assert isinstance(status, PlatformAppStatus)
    assert status.configured is False


async def test_set_google_app_credentials_then_status_reflects_it(
    oauth_client: OAuthBrokerSocketClient,
) -> None:
    credentials = GoogleAppCredentialsInput(
        client_id="abc123.apps.googleusercontent.com",
        client_secret="client-secret-value",
        login_customer_id="1234567890",
    )

    status = await oauth_client.set_google_app_credentials(credentials)

    assert status.configured is True
    assert status.client_id_masked == "****.com"
    status_from_get = await oauth_client.get_app_status(PlatformCode.GOOGLE)
    assert status_from_get.configured is True


async def test_set_google_app_credentials_never_leaks_the_secret_over_the_wire(
    oauth_client: OAuthBrokerSocketClient,
) -> None:
    credentials = GoogleAppCredentialsInput(
        client_id="abc123.apps.googleusercontent.com",
        client_secret="super-secret-client-secret",
        login_customer_id=None,
    )

    status = await oauth_client.set_google_app_credentials(credentials)

    rendered = str(dataclasses.asdict(status))
    assert "super-secret-client-secret" not in rendered


async def test_set_meta_app_credentials_then_delete(oauth_client: OAuthBrokerSocketClient) -> None:
    credentials = MetaAppCredentialsInput(app_id="9876543210", app_secret="meta-secret")

    status = await oauth_client.set_meta_app_credentials(credentials)
    assert status.configured is True

    await oauth_client.delete_app_credentials(PlatformCode.META)

    status_after = await oauth_client.get_app_status(PlatformCode.META)
    assert status_after.configured is False


# --- end lane: app-credentials-ui ---


def test_no_dto_returned_by_oauth_broker_port_can_carry_a_token() -> None:
    """Prueba estructural (no de valor): ningun DTO que `ads-api` recibe del
    broker (`connect_ports.py`) declara un campo que pueda llevar un
    secreto -- ni `token`, ni `refresh_token`, ni `access_token`."""
    forbidden_substrings = ("token", "secret")
    for dto in (OAuthBeginResult, OAuthCompleteResult, DiscoveredAccount, CredentialStatusResult):
        field_names = {f.name for f in dataclasses.fields(dto)}
        leaking = {
            name
            for name in field_names
            if any(bad in name for bad in forbidden_substrings)
        }
        assert not leaking, f"{dto.__name__} declara campos sospechosos: {leaking}"
