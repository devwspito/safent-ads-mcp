"""Hueco documentado en `composition/broker.py` (owner decision,
app-credentials-ui): antes de este cableado, guardar la app de Google desde
el panel DESPUES de arrancar `ads-broker` no llegaba a las lecturas hasta
reiniciar el proceso. Esta prueba levanta el socket Unix real de
`ads-broker` (`broker.presentation.socket_server.serve`) con
`DynamicPlatformAdapterRegistry` (`broker/infrastructure/
dynamic_platform_adapters.py`) y un doble del SDK (contract:
"SDK mocked") y recorre el ciclo completo por el socket, sin reiniciar
nada:

1. Sin credenciales -- `fetch_account_inventory` deniega tipado
   `PLATFORM_APP_NOT_CONFIGURED` (nunca `FAILED` generico, nunca una
   respuesta inventada).
2. `set_platform_app_credentials` (el propietario teclea la app desde el
   panel) -- `get_platform_app_status` refleja `configured: true` de
   inmediato.
3. La SIGUIENTE `fetch_account_inventory` -- el proximo tick de
   `IngestionCycle` en produccion -- ya tiene exito, sin reiniciar el
   broker.
4. `delete_platform_app_credentials` -- `get_platform_app_status` vuelve a
   `configured: false` y la siguiente lectura vuelve a denegar tipado."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from safent_ads.accounts.application.ports import (
    AccountRef,
    AdEntitySnapshot,
    AssetUploadRequest,
    EntityStateSnapshot,
    IdempotencyKey,
    MetricFactSnapshot,
    MetricsRequest,
    PlatformAssetHandle,
    SignedAuthorization,
    WriteIntent,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.broker.application.app_credentials_service import AppCredentialsService
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.infrastructure.dynamic_platform_adapters import (
    DynamicPlatformAdapterRegistry,
)
from safent_ads.broker.platforms.dynamic_oauth_adapters import (
    DynamicGoogleOAuthAdapter,
    DynamicMetaOAuthAdapter,
)
from safent_ads.broker.presentation.dispatcher import BrokerRuntime
from safent_ads.broker.presentation.socket_server import serve
from safent_ads.broker.presentation.wire_protocol import FrameClient
from safent_ads.shared.clock import SystemClock
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode

pytestmark = pytest.mark.integration

_KEY_B64 = base64.b64encode(b"7" * 32).decode()
_NOW = datetime(2026, 9, 10, tzinfo=UTC)
_ACCOUNT_REF = AccountRef(PlatformCode.GOOGLE, "123-456-7890")
_CAMPAIGN_REF = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "campaign-1")
_ACCOUNT_PARENT_REF = EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "123-456-7890")

_CLIENT_ID = "integration-test-client-id"
_CLIENT_SECRET = "integration-test-client-secret"  # noqa: S105


class _UnreachableHttpClient:
    """El flujo OAuth "Conectar" no se ejercita en esta prueba: si algo
    llegara a llamar a la red, es un fallo del test, no un doble legitimo."""

    async def post_form(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        raise AssertionError("no deberia llamar a la red")

    async def get_json(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        raise AssertionError("no deberia llamar a la red")

    async def post_json(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        raise AssertionError("no deberia llamar a la red")


class _StubGoogleAdsAdapter:
    """Doble del SDK real (contract: "SDK mocked"): en vez de
    `LiveGoogleAdsSearchClient`, un inventario fijo -- basta para probar
    que el registro dinamico llega a resolver un adaptador de verdad tras
    guardar credenciales por el socket."""

    async def fetch_account_inventory(
        self,
        account_ref: AccountRef,  # noqa: ARG002 - forma exacta del puerto
    ) -> Sequence[AdEntitySnapshot]:
        return [
            AdEntitySnapshot(
                entity_ref=_CAMPAIGN_REF,
                parent_ref=_ACCOUNT_PARENT_REF,
                name="Campana Otono",
                status=AdEntityStatus.ACTIVE,
                is_controllable=True,
                learning_state=LearningState.NOT_APPLICABLE,
                budget=None,
                bid_target=None,
                shared_budget_ref=None,
                canonical_state={"status": "ACTIVE"},
                fetched_at=_NOW,
            )
        ]

    async def fetch_metrics(
        self,
        request: MetricsRequest,  # noqa: ARG002 - forma exacta del puerto
    ) -> Sequence[MetricFactSnapshot]:
        return []

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot:
        return EntityStateSnapshot(
            entity_ref=entity_ref,
            status=AdEntityStatus.ACTIVE,
            is_controllable=True,
            canonical_state={"status": "ACTIVE"},
            fetched_at=_NOW,
        )

    async def upload_asset(
        self,
        request: AssetUploadRequest,  # noqa: ARG002 - forma exacta del puerto
    ) -> PlatformAssetHandle:
        raise NotImplementedError

    async def run_gaql(
        self,
        account_ref: AccountRef,  # noqa: ARG002 - forma exacta del puerto
        query: str,  # noqa: ARG002
        *,
        max_rows: int,  # noqa: ARG002
    ) -> Sequence[Mapping[str, Any]]:
        return []

    async def execute_write(
        self,
        intent: WriteIntent,  # noqa: ARG002 - forma exacta del puerto
        authorization: SignedAuthorization,  # noqa: ARG002
        idempotency_key: IdempotencyKey,  # noqa: ARG002
    ) -> WriteOutcome:
        raise NotImplementedError


def _runtime(store: EncryptedCredentialStore) -> BrokerRuntime:
    clock = SystemClock()
    google_registry = DynamicPlatformAdapterRegistry(
        store=store,
        google_factory=lambda _secrets: _StubGoogleAdsAdapter(),
        meta_factory=lambda _secrets: _StubGoogleAdsAdapter(),
        google_fallback=None,
        meta_fallback=None,
        google_egress_allowed=True,
        meta_egress_allowed=True,
    )
    google_oauth = DynamicGoogleOAuthAdapter(store, _UnreachableHttpClient(), clock)  # type: ignore[arg-type]
    meta_oauth = DynamicMetaOAuthAdapter(store, _UnreachableHttpClient(), clock)  # type: ignore[arg-type]
    return BrokerRuntime(
        adapters=PlatformAdapterRegistry(adapters=google_registry),
        oauth_flow=OAuthConnectFlow(store, google_oauth, meta_oauth, clock),
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


async def _fetch_google_inventory(socket_path: Path) -> dict[str, object]:
    return await _exchange(
        socket_path,
        {
            "op": "fetch_account_inventory",
            "platform": _ACCOUNT_REF.platform.value,
            "external_account_id": _ACCOUNT_REF.external_account_id,
        },
    )


@pytest.fixture
async def running_broker(tmp_path: Path) -> AsyncIterator[tuple[Path, EncryptedCredentialStore]]:
    store = EncryptedCredentialStore(tmp_path / "credentials", _KEY_B64)
    socket_path = tmp_path / "broker.sock"
    server = await serve(socket_path, _runtime(store), frozenset({os.getuid()}))
    yield socket_path, store
    server.close()
    await server.wait_closed()


async def test_reads_deny_typed_before_the_owner_saves_credentials(
    running_broker: tuple[Path, EncryptedCredentialStore],
) -> None:
    socket_path, _store = running_broker

    response = await _fetch_google_inventory(socket_path)

    assert response == {
        "ok": False,
        "error_code": "PLATFORM_APP_NOT_CONFIGURED",
        "reason": "denied",
    }


async def test_status_starts_unconfigured(
    running_broker: tuple[Path, EncryptedCredentialStore],
) -> None:
    socket_path, _store = running_broker

    response = await _exchange(socket_path, {"op": "get_platform_app_status", "platform": "google"})

    assert response["ok"] is True
    assert response["result"]["configured"] is False  # type: ignore[index]


async def test_saving_credentials_over_the_socket_is_picked_up_on_the_next_read(
    running_broker: tuple[Path, EncryptedCredentialStore],
) -> None:
    """El escenario central del hueco: `set_platform_app_credentials` llega
    por el MISMO socket que usaria el panel -- la siguiente lectura, sin
    reiniciar `ads-broker`, ya tiene exito."""
    socket_path, _store = running_broker
    denied_before = await _fetch_google_inventory(socket_path)
    assert denied_before["error_code"] == "PLATFORM_APP_NOT_CONFIGURED"

    save_response = await _exchange(
        socket_path,
        {
            "op": "set_platform_app_credentials",
            "platform": "google",
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
        },
    )
    assert save_response["ok"] is True
    assert save_response["result"]["configured"] is True  # type: ignore[index]

    status_response = await _exchange(
        socket_path, {"op": "get_platform_app_status", "platform": "google"}
    )
    assert status_response["result"]["configured"] is True  # type: ignore[index]

    inventory_response = await _fetch_google_inventory(socket_path)

    assert inventory_response["ok"] is True
    result = inventory_response["result"]
    assert result == [  # type: ignore[comparison-overlap]
        {
            "entity_ref": str(_CAMPAIGN_REF),
            "parent_ref": str(_ACCOUNT_PARENT_REF),
            "name": "Campana Otono",
            "status": "active",
            "is_controllable": True,
            "learning_state": "not_applicable",
            "budget": None,
            "bid_target": None,
            "shared_budget_ref": None,
            "canonical_state": {"status": "ACTIVE"},
            "fetched_at": _NOW.isoformat(),
        }
    ]
    assert _CLIENT_SECRET not in json.dumps(save_response)
    assert _CLIENT_SECRET not in json.dumps(status_response)


async def test_deleting_credentials_over_the_socket_denies_the_next_read_again(
    running_broker: tuple[Path, EncryptedCredentialStore],
) -> None:
    socket_path, _store = running_broker
    await _exchange(
        socket_path,
        {
            "op": "set_platform_app_credentials",
            "platform": "google",
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
        },
    )
    allowed = await _fetch_google_inventory(socket_path)
    assert allowed["ok"] is True

    delete_response = await _exchange(
        socket_path, {"op": "delete_platform_app_credentials", "platform": "google"}
    )
    assert delete_response == {"ok": True, "result": {"deleted": True}}

    status_response = await _exchange(
        socket_path, {"op": "get_platform_app_status", "platform": "google"}
    )
    assert status_response["result"]["configured"] is False  # type: ignore[index]

    denied_again = await _fetch_google_inventory(socket_path)

    assert denied_again == {
        "ok": False,
        "error_code": "PLATFORM_APP_NOT_CONFIGURED",
        "reason": "denied",
    }
