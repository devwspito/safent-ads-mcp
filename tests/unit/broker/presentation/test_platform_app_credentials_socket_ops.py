"""Los tres `op` de credenciales de VENDOR (owner decision,
app-credentials-ui) de punta a punta sobre un socket Unix real:
`set_platform_app_credentials`/`get_platform_app_status`/
`delete_platform_app_credentials` -- nunca devuelven el secreto, solo el
estado ya enmascarado."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.platforms.dynamic_oauth_adapters import (
    DynamicGoogleOAuthAdapter,
    DynamicMetaOAuthAdapter,
)
from safent_ads.broker.presentation.dispatcher import BrokerRuntime
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.broker.presentation.wire_protocol import FrameClient
from safent_ads.shared.clock import SystemClock

_KEY_B64 = base64.b64encode(b"0" * 32).decode()

_GOOGLE_CLIENT_SECRET = "super-secret-client-secret"  # noqa: S105
_META_APP_SECRET = "super-secret-meta-app-secret"  # noqa: S105


def _runtime(store_dir: Path) -> BrokerRuntime:
    clock = SystemClock()
    store = EncryptedCredentialStore(store_dir, _KEY_B64)
    google = DynamicGoogleOAuthAdapter(store, _UnreachableHttpClient(), clock)  # type: ignore[arg-type]
    meta = DynamicMetaOAuthAdapter(store, _UnreachableHttpClient(), clock)  # type: ignore[arg-type]
    return BrokerRuntime(
        adapters=PlatformAdapterRegistry({}),
        oauth_flow=OAuthConnectFlow(store, google, meta, clock),
        app_credentials=AppCredentialsService(store, clock),
    )


class _UnreachableHttpClient:
    async def post_form(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        raise AssertionError("no deberia llamar a la red en estos tests")

    async def get_json(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        raise AssertionError("no deberia llamar a la red en estos tests")

    async def post_json(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        raise AssertionError("no deberia llamar a la red en estos tests")


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


def _assert_no_secret_leak(response: dict[str, object]) -> None:
    serialized = json.dumps(response)
    assert _GOOGLE_CLIENT_SECRET not in serialized
    assert _META_APP_SECRET not in serialized


async def test_google_status_starts_unconfigured(running_server: Path) -> None:
    response = await _exchange(
        running_server, {"op": "get_platform_app_status", "platform": "google"}
    )

    assert response["ok"] is True
    assert response["result"]["configured"] is False  # type: ignore[index]


async def test_set_then_get_google_app_credentials_never_leaks_the_secret(
    running_server: Path,
) -> None:
    set_response = await _exchange(
        running_server,
        {
            "op": "set_platform_app_credentials",
            "platform": "google",
            "client_id": "abc123.apps.googleusercontent.com",
            "client_secret": _GOOGLE_CLIENT_SECRET,
            "login_customer_id": "1234567890",
        },
    )

    assert set_response["ok"] is True
    assert set_response["result"]["configured"] is True  # type: ignore[index]
    assert set_response["result"]["client_id_masked"] == "****.com"  # type: ignore[index]
    _assert_no_secret_leak(set_response)

    status_response = await _exchange(
        running_server, {"op": "get_platform_app_status", "platform": "google"}
    )
    assert status_response["result"]["configured"] is True  # type: ignore[index]
    _assert_no_secret_leak(status_response)


async def test_set_then_delete_meta_app_credentials(running_server: Path) -> None:
    await _exchange(
        running_server,
        {
            "op": "set_platform_app_credentials",
            "platform": "meta",
            "app_id": "9876543210",
            "app_secret": _META_APP_SECRET,
        },
    )

    delete_response = await _exchange(
        running_server, {"op": "delete_platform_app_credentials", "platform": "meta"}
    )
    assert delete_response["ok"] is True

    status_response = await _exchange(
        running_server, {"op": "get_platform_app_status", "platform": "meta"}
    )
    assert status_response["result"]["configured"] is False  # type: ignore[index]


async def test_set_google_with_missing_required_field_is_denied(running_server: Path) -> None:
    response = await _exchange(
        running_server,
        {
            "op": "set_platform_app_credentials",
            "platform": "google",
            "client_id": "abc123.apps.googleusercontent.com",
            "client_secret": "",
        },
    )

    assert response["ok"] is False
    assert response["error_code"] == "APP_CREDENTIALS_INCOMPLETE"


async def test_delete_unconfigured_platform_is_a_noop(running_server: Path) -> None:
    response = await _exchange(
        running_server, {"op": "delete_platform_app_credentials", "platform": "google"}
    )

    assert response["ok"] is True
