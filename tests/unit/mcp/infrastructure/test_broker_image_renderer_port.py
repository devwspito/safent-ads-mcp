"""`BrokerImageRenderer` (ads-api, lane 003): serializa `ImageSpec` al
sobre de `render_image` y reconstruye `RenderedAsset` sobre la respuesta
del broker, guardando los bytes decodificados con el `AssetStorePort` real
de `ads-api` (`LocalAssetStorage` en produccion, un doble aqui)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.infrastructure.errors import BrokerConnectionError
from safent_ads.broker.presentation.wire_protocol import FrameClient
from safent_ads.creative.application.errors import (
    RenderBudgetExceededError,
    RenderQuotaExceededError,
)
from safent_ads.creative.application.ports import render_call_scope
from safent_ads.creative.domain.enums import Format, MediaKind, RendererName
from safent_ads.creative.domain.render_specs import ImageSpec
from safent_ads.mcp.infrastructure.broker_image_renderer_port import (
    BrokerImageRenderer,
    ImageRenderBrokerClient,
)
from tests.unit.creative.domain.factories import make_brand_kit
from tests.unit.creative.infrastructure.fakes import FakeAssetStore

_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png-body"
_GENERATED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _spec(**overrides: object) -> ImageSpec:
    defaults: dict[str, object] = {
        "prompt": "un perro feliz en un parque",
        "reference_assets": (),
        "format": Format.SQUARE_1080,
        "seed": 7,
        "brand_kit": make_brand_kit(),
    }
    defaults.update(overrides)
    return ImageSpec(**defaults)  # type: ignore[arg-type]


def _response(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "image_base64": base64.b64encode(_PNG_BYTES).decode("ascii"),
        "content_type": "image/png",
        "model_name": "flux-2-klein-9b",
        "cost": {"amount": "0.02", "currency": "USD"},
        "provenance": {
            "renderer": RendererName.FLUX2_KLEIN_9B.value,
            "checksum": "broker-reported-checksum-never-trusted",
            "generated_at": _GENERATED_AT.isoformat(),
            "duration_s": 1.5,
        },
    }
    body.update(overrides)
    return body


async def test_render_sends_the_expected_wire_payload() -> None:
    client = AsyncMock()
    client.render_image.return_value = _response()
    renderer = BrokerImageRenderer(RendererName.FLUX2_KLEIN_9B, client, FakeAssetStore())
    spec = _spec()

    with render_call_scope("biz-1"):
        await renderer.render(spec)

    sent = client.render_image.await_args.args[0]
    assert sent["op"] == "render_image"
    assert sent["business_id"] == "biz-1"
    assert sent["renderer"] == "flux2_klein_9b"
    assert sent["prompt"] == spec.prompt
    assert sent["format"] == "1080x1080"
    assert sent["seed"] == 7
    assert sent["brand_kit"]["primary_color_hex"] == spec.brand_kit.primary_color_hex
    assert sent["brand_kit"]["logo_asset_id"] == str(spec.brand_kit.logo_asset_id)
    assert sent["brand_kit"]["safe_area_top"] == spec.brand_kit.safe_area.top


async def test_render_stores_the_decoded_bytes_in_the_real_asset_store() -> None:
    client = AsyncMock()
    client.render_image.return_value = _response()
    asset_store = FakeAssetStore()
    renderer = BrokerImageRenderer(RendererName.FLUX2_KLEIN_9B, client, asset_store)

    await renderer.render(_spec())

    assert asset_store.puts == [(_PNG_BYTES, MediaKind.IMAGE)]


async def test_render_recomputes_the_checksum_instead_of_trusting_the_broker() -> None:
    client = AsyncMock()
    client.render_image.return_value = _response()
    renderer = BrokerImageRenderer(RendererName.FLUX2_KLEIN_9B, client, FakeAssetStore())

    rendered = await renderer.render(_spec())

    assert rendered.checksum == hashlib.sha256(_PNG_BYTES).hexdigest()
    assert rendered.checksum != "broker-reported-checksum-never-trusted"


async def test_render_builds_a_rendered_asset_from_the_response() -> None:
    client = AsyncMock()
    client.render_image.return_value = _response()
    renderer = BrokerImageRenderer(RendererName.FLUX2_KLEIN_9B, client, FakeAssetStore())
    spec = _spec()

    rendered = await renderer.render(spec)

    assert rendered.media_kind == MediaKind.IMAGE
    assert rendered.format == spec.format
    assert rendered.renderer_used == RendererName.FLUX2_KLEIN_9B
    assert str(rendered.cost_estimate.amount) == "0.02"
    assert rendered.cost_estimate.currency == "USD"
    assert rendered.duration_s == 1.5
    assert rendered.generated_at == _GENERATED_AT


async def test_render_propagates_a_broker_denial_unwrapped() -> None:
    client = AsyncMock()
    client.render_image.side_effect = BrokerRequestDeniedError(
        "IMAGE_RENDERER_NOT_CONFIGURED", "denied"
    )
    renderer = BrokerImageRenderer(RendererName.FLUX2_KLEIN_9B, client, FakeAssetStore())

    with pytest.raises(BrokerRequestDeniedError):
        await renderer.render(_spec())


async def test_render_propagates_a_transport_failure_unwrapped() -> None:
    client = AsyncMock()
    client.render_image.side_effect = BrokerConnectionError("fallo de E/S hablando con el broker")
    renderer = BrokerImageRenderer(RendererName.FLUX2_KLEIN_9B, client, FakeAssetStore())

    with pytest.raises(BrokerConnectionError):
        await renderer.render(_spec())


async def test_render_translates_a_quota_denial_into_a_creative_error() -> None:
    """M-2 (revision de seguridad 0.2.22): `RENDER_QUOTA_EXCEEDED` del
    broker se traduce a `RenderQuotaExceededError` de `creative` -- nunca
    un `BrokerRequestDeniedError` crudo de `accounts` cruzando al
    llamador (`GenerateCreativeAssets`), que para la cascada en vez de
    probar el siguiente candidato."""
    client = AsyncMock()
    client.render_image.side_effect = BrokerRequestDeniedError("RENDER_QUOTA_EXCEEDED", "denied")
    renderer = BrokerImageRenderer(RendererName.FLUX2_KLEIN_9B, client, FakeAssetStore())

    with pytest.raises(RenderQuotaExceededError):
        await renderer.render(_spec())


async def test_render_translates_a_cost_cap_denial_into_a_creative_error() -> None:
    """M-2: `RENDER_COST_CAP` se traduce a `RenderBudgetExceededError`
    (mismo tipo que el chequeo post-render existente de
    `GenerateCreativeAssets._require_within_budget`) -- un solo tipo de
    error que el llamador ya sabe interpretar como "aborta, no sigas"."""
    client = AsyncMock()
    client.render_image.side_effect = BrokerRequestDeniedError("RENDER_COST_CAP", "denied")
    renderer = BrokerImageRenderer(RendererName.FLUX2_KLEIN_9B, client, FakeAssetStore())

    with pytest.raises(RenderBudgetExceededError):
        await renderer.render(_spec())


async def _serve_cold_start(socket_path: Path, *, slow_seconds: float) -> list[int]:
    """Servidor de pega: SIEMPRE tarda `slow_seconds` en responder (no solo
    la primera vez, a proposito -- esta prueba nunca espera una segunda
    conexion)."""
    calls: list[int] = [0]

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        frame_client = FrameClient(reader, writer, max_frame_bytes=64 * 1024)
        await frame_client.read_frame()
        calls[0] += 1
        await asyncio.sleep(slow_seconds)
        await frame_client.write_frame(json.dumps({"ok": True, "result": {}}).encode("utf-8"))
        frame_client.close()
        await frame_client.wait_closed()

    server = await asyncio.start_unix_server(handler, path=str(socket_path))
    try:
        client = ImageRenderBrokerClient(socket_path)
        client._timeout_seconds = 0.2  # noqa: SLF001 - pequeno a proposito, ver docstring
        with pytest.raises(BrokerConnectionError):
            await client.render_image({"op": "render_image"})
    finally:
        server.close()
        await server.wait_closed()
    return calls


async def test_render_image_is_never_retried_on_a_cold_broker(tmp_path: Path) -> None:
    """Regresion (16-sep, companion 0.2.32): a diferencia de una lectura,
    `render_image` gasta dinero real y consume la cuota por negocio ANTES
    de que la respuesta cruce el socket -- un `BrokerConnectionError` debe
    seguir siendo un fallo real, sin una segunda conexion automatica que
    repetiria el gasto."""
    calls = await _serve_cold_start(tmp_path / "broker.sock", slow_seconds=1.0)

    assert calls == [1]
