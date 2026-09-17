"""`ComfyUiImageRenderer`/`ComfyUiVideoRenderer` contra un servidor HTTP
falso (`httpx.MockTransport`, sin red real ni contenedor GPU — este carril
nunca arranca ComfyUI de verdad, threat-model.md C-29). Cubre T102
(`test_single_heavy_job` vive en `test_in_process_gpu_queue.py`) y las dos
pruebas nombradas por `contracts/creative-port.md`:
`test_comfyui_placeholder_substitution`, `test_comfyui_timeout`."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from safent_ads.creative.domain.enums import Format, MediaKind, RendererName
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset, VideoSpec
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.creative.infrastructure.comfyui_adapter import (
    ComfyUiImageRenderer,
    ComfyUiRenderError,
    ComfyUiTimeoutError,
    ComfyUiVideoRenderer,
    CreativeLocalRenderingDisabledError,
    fill_workflow_template,
)
from safent_ads.shared.clock import FixedClock
from tests.unit.creative.domain.factories import make_brand_kit
from tests.unit.creative.infrastructure.fakes import (
    FakeAssetRetrieval,
    FakeAssetStore,
    FakeCreativeAssetRepository,
    make_creative_asset,
)

_WORKFLOWS_DIR = Path(__file__).parents[4] / "infra" / "creative" / "workflows"
_KLEIN_TEMPLATE = (_WORKFLOWS_DIR / "image_fast_klein.json").read_text()


def test_placeholder_substitution_produces_valid_json_and_exact_values() -> None:
    graph = fill_workflow_template(
        _KLEIN_TEMPLATE,
        prompt='Escena "con comillas" y ñ',
        width=1080,
        height=1350,
        seed=42,
    )

    text_node = graph["4"]["inputs"]["text"]  # type: ignore[index]
    latent_node = graph["8"]["inputs"]  # type: ignore[index]
    noise_node = graph["6"]["inputs"]["noise_seed"]  # type: ignore[index]
    assert text_node == 'Escena "con comillas" y ñ'
    assert latent_node["width"] == 1080
    assert latent_node["height"] == 1350
    assert noise_node == 42


def test_placeholder_substitution_keeps_untouched_nodes_intact() -> None:
    graph = fill_workflow_template(_KLEIN_TEMPLATE, prompt="x", width=1, height=1, seed=1)

    assert graph["1"]["class_type"] == "UNETLoader"  # type: ignore[index]


def test_placeholder_substitution_with_image_filename() -> None:
    template = json.dumps({"6": {"inputs": {"image": "{{image}}"}}})

    graph = fill_workflow_template(
        template, prompt="p", width=1, height=1, seed=1, image_filename="frame.png"
    )

    assert graph["6"]["inputs"]["image"] == "frame.png"  # type: ignore[index]


def _history_response(prompt_id: str, outputs: dict[str, object] | None) -> dict[str, object]:
    if outputs is None:
        return {prompt_id: {"outputs": {}}}
    return {prompt_id: {"outputs": outputs}}


def _make_image_renderer(
    handler: httpx.MockTransport, asset_store: FakeAssetStore, *, timeout_s: float = 5.0
) -> ComfyUiImageRenderer:
    http_client = httpx.AsyncClient(transport=handler)
    return ComfyUiImageRenderer(
        http_client,
        base_url="http://127.0.0.1:8188",
        workflow_template=_KLEIN_TEMPLATE,
        name=RendererName.FLUX2_KLEIN,
        asset_store=asset_store,
        clock=FixedClock(datetime.now(UTC)),
        timeout_s=timeout_s,
        local_enabled=True,
        poll_interval_s=0.01,
    )


def test_comfyui_image_renderer_fails_closed_when_local_disabled() -> None:
    with pytest.raises(CreativeLocalRenderingDisabledError, match="9-sep-2026"):
        ComfyUiImageRenderer(
            httpx.AsyncClient(),
            base_url="http://127.0.0.1:8188",
            workflow_template=_KLEIN_TEMPLATE,
            name=RendererName.FLUX2_KLEIN,
            asset_store=FakeAssetStore(),
            clock=FixedClock(datetime.now(UTC)),
            timeout_s=5.0,
            local_enabled=False,
        )


def test_comfyui_render_happy_path() -> None:
    async def _run() -> RenderedAsset:
        outputs = {"13": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": "prompt-123"})
            if request.url.path == "/history/prompt-123":
                return httpx.Response(200, json=_history_response("prompt-123", outputs))
            if request.url.path == "/view":
                return httpx.Response(200, content=b"\xff\xd8\xffPNGBYTES")
            raise AssertionError(f"unexpected path {request.url.path}")

        asset_store = FakeAssetStore()
        renderer = _make_image_renderer(httpx.MockTransport(handler), asset_store)
        spec = ImageSpec(
            prompt="Estudiante en biblioteca",
            reference_assets=(),
            format=Format.SQUARE_1080,
            seed=7,
            brand_kit=make_brand_kit(),
        )
        return await renderer.render(spec)

    rendered = asyncio.run(_run())

    assert rendered.renderer_used == RendererName.FLUX2_KLEIN
    assert rendered.media_kind == MediaKind.IMAGE
    assert rendered.checksum
    assert rendered.cost_estimate == Money.zero("USD")


def test_comfyui_timeout_raises_when_history_never_completes() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": "prompt-123"})
            if request.url.path == "/history/prompt-123":
                return httpx.Response(200, json=_history_response("prompt-123", None))
            raise AssertionError(f"unexpected path {request.url.path}")

        renderer = _make_image_renderer(
            httpx.MockTransport(handler), FakeAssetStore(), timeout_s=0.05
        )
        spec = ImageSpec(
            prompt="Estudiante en biblioteca",
            reference_assets=(),
            format=Format.SQUARE_1080,
            seed=7,
            brand_kit=make_brand_kit(),
        )
        await renderer.render(spec)

    with pytest.raises(ComfyUiTimeoutError):
        asyncio.run(_run())


def test_comfyui_missing_prompt_id_raises() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        renderer = _make_image_renderer(httpx.MockTransport(handler), FakeAssetStore())
        spec = ImageSpec(
            prompt="x",
            reference_assets=(),
            format=Format.SQUARE_1080,
            seed=1,
            brand_kit=make_brand_kit(),
        )
        await renderer.render(spec)

    with pytest.raises(ComfyUiRenderError):
        asyncio.run(_run())


def test_video_renderer_uploads_key_frame_before_enqueue() -> None:
    async def _run() -> RenderedAsset:
        upload_calls: list[str] = []
        enqueue_seen_image: list[object] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/upload/image":
                upload_calls.append("uploaded")
                return httpx.Response(200, json={"name": "confirmed.png"})
            if request.url.path == "/prompt":
                body = json.loads(request.content)
                enqueue_seen_image.append(body["prompt"]["6"]["inputs"]["image"])
                return httpx.Response(200, json={"prompt_id": "vid-1"})
            if request.url.path == "/history/vid-1":
                outputs = {"20": {"videos": [{"filename": "out.mp4", "type": "output"}]}}
                return httpx.Response(200, json=_history_response("vid-1", outputs))
            if request.url.path == "/view":
                return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42")
            raise AssertionError(f"unexpected path {request.url.path}")

        key_frame_id = AssetId.new()
        key_frame_uri = StorageUri("image/keyframe.png")
        assets = FakeCreativeAssetRepository(
            {key_frame_id: make_creative_asset(asset_id=key_frame_id, storage_uri=key_frame_uri)}
        )
        asset_retrieval = FakeAssetRetrieval({key_frame_uri.key: b"raw-image-bytes"})
        asset_store = FakeAssetStore()
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        renderer = ComfyUiVideoRenderer(
            http_client,
            base_url="http://127.0.0.1:8188",
            workflow_template=json.dumps({"6": {"inputs": {"image": "{{image}}"}}}),
            name=RendererName.LTX_2_5,
            asset_store=asset_store,
            asset_retrieval=asset_retrieval,
            assets=assets,
            clock=FixedClock(datetime.now(UTC)),
            timeout_s=5.0,
            local_enabled=True,
            poll_interval_s=0.01,
        )
        spec = VideoSpec(
            key_frames=(key_frame_id,),
            motion_prompt="camara lenta",
            duration_s=5,
            format=Format.STORY_1080X1920,
            seed=3,
        )
        result = await renderer.render(spec)
        assert upload_calls == ["uploaded"]
        assert enqueue_seen_image == ["confirmed.png"]
        return result

    rendered = asyncio.run(_run())

    assert rendered.media_kind == MediaKind.VIDEO
    assert rendered.duration_s == 5.0
