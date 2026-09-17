"""`tools.broker_probe._send`: el comando "sin firmar" que
`tests/smoke/f0_smoke.sh` usa como prueba de humo -- sin el objeto
`authorization`, el esquema tipado del bróker lo rechaza antes de que
ningún adaptador lo vea, sin importar que plataformas esten registradas."""

from __future__ import annotations

import base64
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

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
from safent_ads.tools.broker_probe import _UNSIGNED_WRITE_REQUEST, _send

_NOW = datetime(2026, 9, 9, tzinfo=UTC)
_CREDENTIAL_MASTER_KEY_B64 = base64.b64encode(b"0" * 32).decode()


class _UnreachableHttpClient:
    """Esta sonda nunca ejercita un `op` de OAuth: si algo llegara a llamar
    al cliente HTTP, es un fallo del banco, no un doble legitimo."""

    async def post_form(self, url: str, *, data: dict[str, str]) -> dict[str, object]:  # noqa: ARG002
        raise AssertionError

    async def get_json(
        self, url: str, *, headers: object = None, params: object = None  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError

    async def post_json(
        self, url: str, *, json_body: object, headers: object = None  # noqa: ARG002
    ) -> dict[str, object]:
        raise AssertionError


def _runtime(store_dir: Path) -> BrokerRuntime:
    clock = FixedClock(_NOW)
    store = EncryptedCredentialStore(store_dir, _CREDENTIAL_MASTER_KEY_B64)
    google = GoogleOAuthAdapter(
        GoogleOAuthAdapterConfig(client_id="c", client_secret="s"),
        _UnreachableHttpClient(),  # type: ignore[arg-type]
        clock,
    )
    meta = MetaOAuthAdapter(
        MetaOAuthAdapterConfig(app_id="a", app_secret="s"), _UnreachableHttpClient(), clock  # type: ignore[arg-type]
    )
    # Registro vacio a proposito: la validacion del esquema rechaza el
    # comando sin firmar ANTES de que el bróker resuelva ningun adaptador.
    registry = PlatformAdapterRegistry({})
    return BrokerRuntime(
        adapters=registry,
        oauth_flow=OAuthConnectFlow(store, google, meta, clock),
        app_credentials=AppCredentialsService(store, clock),
    )


async def test_an_unsigned_write_command_is_denied_before_any_adapter_is_resolved() -> None:
    async def scenario(socket_path: Path) -> dict[str, object]:
        runtime = _runtime(socket_path.parent / "credentials")
        server = await serve(socket_path, runtime, frozenset({os.getuid()}))
        try:
            return await _send(str(socket_path), _UNSIGNED_WRITE_REQUEST)
        finally:
            server.close()
            await server.wait_closed()

    with tempfile.TemporaryDirectory() as tmp_dir:
        response = await scenario(Path(tmp_dir) / "broker.sock")

    assert response == {"ok": False, "error_code": "DENIED", "reason": "invalid_schema"}
