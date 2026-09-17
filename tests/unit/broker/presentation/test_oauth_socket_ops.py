"""Los cinco `op` de conexion OAuth (US3) de punta a punta sobre un socket
Unix real: solo el HTTP saliente a Google/Meta esta mockeado
(contracts/rest-api.md §Conexiones: "Ningun token llega al navegador" — ni,
comprobado aqui, a `ads-api`)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

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
from safent_ads.broker.presentation.wire_protocol import FrameClient
from safent_ads.shared.clock import FixedClock

_KEY_B64 = base64.b64encode(b"0" * 32).decode()
_GOOGLE_REFRESH_TOKEN = "1//super-secret-refresh-token"  # noqa: S105
_META_LONG_LIVED_TOKEN = "EAA-super-secret-long-lived-token"  # noqa: S105
_META_SYSTEM_USER_TOKEN = "pasted-system-user-token"  # noqa: S105

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
_GOOGLE_LIST_CUSTOMERS_URL = (
    "https://googleads.googleapis.com/v25/customers:listAccessibleCustomers"
)
_GOOGLE_SEARCH_URL = "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:search"
_META_TOKEN_URL = "https://graph.facebook.com/v26.0/oauth/access_token"  # noqa: S105
_META_ADACCOUNTS_URL = "https://graph.facebook.com/v26.0/me/adaccounts"
_META_ME_URL = "https://graph.facebook.com/v26.0/me"


class _ScriptedHttpClient:
    """Doble de `OAuthHttpClient`: respuestas fijas por endpoint, igual que
    los tests de `google_oauth_adapter`/`meta_oauth_adapter`."""

    def __init__(self) -> None:
        self._get_json_by_url: dict[str, Mapping[str, Any]] = {
            _GOOGLE_LIST_CUSTOMERS_URL: {"resourceNames": ["customers/1234567890"]},
            f"{_META_TOKEN_URL}#1": {"access_token": "short-lived"},
            f"{_META_TOKEN_URL}#2": {"access_token": _META_LONG_LIVED_TOKEN, "expires_in": 5184000},
            _META_ADACCOUNTS_URL: {
                "data": [
                    {
                        "account_id": "act_555",
                        "name": "Cuenta Meta",
                        "currency": "EUR",
                        "timezone_name": "Europe/Madrid",
                    }
                ]
            },
            _META_ME_URL: {"id": "10000000"},
        }
        self._meta_token_calls = 0

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
        if url == _META_TOKEN_URL:
            self._meta_token_calls += 1
            return self._get_json_by_url[f"{_META_TOKEN_URL}#{self._meta_token_calls}"]
        return self._get_json_by_url[url]

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


async def _exchange(socket_path: Path, request: dict[str, object]) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    client = FrameClient(reader, writer, max_frame_bytes=64 * 1024)
    await client.write_frame(json.dumps(request).encode("utf-8"))
    response: dict[str, object] = json.loads(await client.read_frame())
    client.close()
    await client.wait_closed()
    return response


@pytest.fixture
async def running_server(tmp_path: Path) -> AsyncIterator[Path]:
    socket_path = tmp_path / "broker.sock"
    server = await serve(socket_path, _runtime(tmp_path / "credentials"), frozenset({os.getuid()}))
    yield socket_path
    server.close()
    await server.wait_closed()


def _assert_no_token_leak(response: dict[str, object]) -> None:
    serialized = json.dumps(response)
    assert _GOOGLE_REFRESH_TOKEN not in serialized
    assert _META_LONG_LIVED_TOKEN not in serialized
    assert _META_SYSTEM_USER_TOKEN not in serialized


async def test_google_connect_round_trip_never_leaks_the_token(running_server: Path) -> None:
    begin = await _exchange(
        running_server,
        {
            "op": "oauth_begin",
            "provider": "google",
            "business_id": "biz-1",
            "redirect_uri": "https://x/callback",
        },
    )
    assert begin["ok"] is True
    state = begin["result"]["state"]  # type: ignore[index]
    _assert_no_token_leak(begin)

    complete = await _exchange(
        running_server, {"op": "oauth_complete", "state": state, "code": "auth-code"}
    )

    assert complete["ok"] is True
    accounts = complete["result"]["accounts"]  # type: ignore[index]
    assert accounts[0]["external_account_id"] == "1234567890"
    assert accounts[0]["currency"] == "EUR"
    _assert_no_token_leak(complete)


async def test_oauth_complete_is_single_use_over_the_socket(running_server: Path) -> None:
    begin = await _exchange(
        running_server,
        {
            "op": "oauth_begin",
            "provider": "meta",
            "business_id": "biz-1",
            "redirect_uri": "https://x/callback",
        },
    )
    state = begin["result"]["state"]  # type: ignore[index]
    await _exchange(running_server, {"op": "oauth_complete", "state": state, "code": "auth-code"})

    replay = await _exchange(
        running_server, {"op": "oauth_complete", "state": state, "code": "auth-code"}
    )

    assert replay["ok"] is False
    assert replay["error_code"] == "OAUTH_SESSION_NOT_FOUND"


async def test_credential_status_then_revoke(running_server: Path) -> None:
    begin = await _exchange(
        running_server,
        {
            "op": "oauth_begin",
            "provider": "google",
            "business_id": "biz-1",
            "redirect_uri": "https://x/callback",
        },
    )
    complete = await _exchange(
        running_server,
        {"op": "oauth_complete", "state": begin["result"]["state"], "code": "auth-code"},  # type: ignore[index]
    )
    credential_ref_id = complete["result"]["accounts"][0]["credential_ref_id"]  # type: ignore[index]

    status_before = await _exchange(
        running_server, {"op": "credential_status", "credential_ref_id": credential_ref_id}
    )
    assert status_before["result"]["status"] == "connected"  # type: ignore[index]

    revoke = await _exchange(
        running_server, {"op": "revoke_credential", "credential_ref_id": credential_ref_id}
    )
    assert revoke["ok"] is True

    status_after = await _exchange(
        running_server, {"op": "credential_status", "credential_ref_id": credential_ref_id}
    )
    assert status_after["result"]["status"] == "revoked"  # type: ignore[index]


async def test_credential_status_unknown_id_is_denied(running_server: Path) -> None:
    response = await _exchange(
        running_server,
        {
            "op": "credential_status",
            "credential_ref_id": "00000000-0000-0000-0000-000000000000",
        },
    )

    assert response["ok"] is False
    assert response["error_code"] == "CREDENTIAL_NOT_FOUND"


async def test_register_meta_system_user_token_over_the_socket(running_server: Path) -> None:
    response = await _exchange(
        running_server,
        {"op": "register_meta_system_user_token", "token": _META_SYSTEM_USER_TOKEN},
    )

    assert response["ok"] is True
    accounts = response["result"]["accounts"]  # type: ignore[index]
    assert accounts[0]["external_account_id"] == "act_555"
    _assert_no_token_leak(response)
