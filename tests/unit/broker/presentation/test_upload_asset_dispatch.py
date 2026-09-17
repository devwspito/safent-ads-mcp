"""`upload_asset` (M-3, revision de seguridad 0.2.22) enrutado de extremo a
extremo -- `handle_payload()` con un `PlatformAdapterRegistry` real sobre
un `AdsPlatformPort` doble, mismo criterio que
`test_render_image_dispatch.py`: se prueba contra el sobre JSON completo,
como lo vera `ads-api` de verdad. Los bytes viajan en base64 DENTRO de la
peticion (al reves que `render_image`, que los devuelve en la respuesta)
-- por eso `socket_server.py` deja crecer la LECTURA hasta 16 MiB solo
para esta `op` y `render_image`."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from types import SimpleNamespace

from safent_ads.accounts.application.ports import (
    AccountRef,
    AdEntitySnapshot,
    AdsPlatformPort,
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
from safent_ads.broker.infrastructure.adapter_registry import PlatformAdapterRegistry
from safent_ads.broker.presentation.dispatcher import BrokerRuntime, handle_payload
from safent_ads.shared.ids import EntityRef, PlatformCode

_MEDIA_BYTES = b"\x89PNG\r\n\x1a\nfake-png-body"


class _FakeAdsPlatformPort(AdsPlatformPort):
    def __init__(self, *, deny: Exception | None = None) -> None:
        self.upload_asset_calls: list[AssetUploadRequest] = []
        self._deny = deny

    async def fetch_account_inventory(
        self, account_ref: AccountRef  # noqa: ARG002 - forma exacta del puerto
    ) -> Sequence[AdEntitySnapshot]:
        raise NotImplementedError

    async def fetch_metrics(
        self, request: MetricsRequest  # noqa: ARG002 - forma exacta del puerto
    ) -> Sequence[MetricFactSnapshot]:
        raise NotImplementedError

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot:
        raise NotImplementedError

    async def upload_asset(self, request: AssetUploadRequest) -> PlatformAssetHandle:
        if self._deny is not None:
            raise self._deny
        self.upload_asset_calls.append(request)
        return PlatformAssetHandle(
            platform_asset_id="asset-1", preview_url="https://cdn.example/asset-1"
        )

    async def run_gaql(
        self, account_ref: AccountRef, query: str, *, max_rows: int  # noqa: ARG002
    ) -> Sequence[dict[str, object]]:
        raise NotImplementedError

    async def execute_write(
        self,
        intent: WriteIntent,  # noqa: ARG002 - forma exacta del puerto
        authorization: SignedAuthorization,  # noqa: ARG002
        idempotency_key: IdempotencyKey,  # noqa: ARG002
    ) -> WriteOutcome:
        raise NotImplementedError


def _runtime(adapter: AdsPlatformPort) -> BrokerRuntime:
    return BrokerRuntime(
        adapters=PlatformAdapterRegistry({PlatformCode.GOOGLE: adapter}),
        oauth_flow=SimpleNamespace(),  # type: ignore[arg-type] - no exercised aqui
        app_credentials=SimpleNamespace(),  # type: ignore[arg-type]
    )


def _payload(**overrides: object) -> bytes:
    body: dict[str, object] = {
        "op": "upload_asset",
        "platform": "google",
        "external_account_id": "123-456-7890",
        "file_name": "banner.png",
        "mime_type": "image/png",
        "media_base64": base64.b64encode(_MEDIA_BYTES).decode("ascii"),
    }
    body.update(overrides)
    return json.dumps(body).encode()


async def test_upload_asset_end_to_end() -> None:
    adapter = _FakeAdsPlatformPort()
    runtime = _runtime(adapter)

    response = json.loads(await handle_payload(_payload(), runtime))

    assert response == {
        "ok": True,
        "result": {
            "platform_asset_id": "asset-1",
            "preview_url": "https://cdn.example/asset-1",
        },
    }
    assert len(adapter.upload_asset_calls) == 1
    sent = adapter.upload_asset_calls[0]
    assert sent.account_ref.platform is PlatformCode.GOOGLE
    assert sent.account_ref.external_account_id == "123-456-7890"
    assert sent.file_name == "banner.png"
    assert sent.mime_type == "image/png"
    assert sent.media == _MEDIA_BYTES


async def test_upload_asset_rejects_a_missing_required_field() -> None:
    runtime = _runtime(_FakeAdsPlatformPort())
    body = json.loads(_payload())
    del body["file_name"]

    response = json.loads(await handle_payload(json.dumps(body).encode(), runtime))

    assert response == {"ok": False, "error_code": "DENIED", "reason": "invalid_schema"}


async def test_upload_asset_rejects_an_empty_media_field() -> None:
    runtime = _runtime(_FakeAdsPlatformPort())

    response = json.loads(await handle_payload(_payload(media_base64=""), runtime))

    assert response == {"ok": False, "error_code": "DENIED", "reason": "invalid_schema"}


async def test_upload_asset_rejects_an_unregistered_platform() -> None:
    """Solo `google` esta registrado en este runtime -- `meta` cae en el
    generico `FAILED`/`adapter_error`, nunca en un adaptador a medias."""
    runtime = _runtime(_FakeAdsPlatformPort())

    response = json.loads(await handle_payload(_payload(platform="meta"), runtime))

    assert response == {"ok": False, "error_code": "FAILED", "reason": "adapter_error"}


async def test_upload_asset_adapter_failure_is_denied_generically() -> None:
    adapter = _FakeAdsPlatformPort(deny=RuntimeError("upstream boom"))
    runtime = _runtime(adapter)

    response = json.loads(await handle_payload(_payload(), runtime))

    assert response == {"ok": False, "error_code": "FAILED", "reason": "adapter_error"}
