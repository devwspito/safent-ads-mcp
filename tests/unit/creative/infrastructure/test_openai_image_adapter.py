"""`OpenAiImageRenderer` contra un servidor HTTP falso
(`httpx.MockTransport`, sin red real): tamano pedido calculado
(multiplo de 16, nunca ampliar), recorte generico a la relacion de
aspecto exacta sobre las dimensiones REALES devueltas, coste leido de
`usage`, modelo configurable."""

from __future__ import annotations

import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from safent_ads.creative.domain.enums import Format, RendererName
from safent_ads.creative.domain.render_specs import ImageSpec
from safent_ads.creative.infrastructure.openai_image_adapter import (
    ImageBackendCapabilities,
    OpenAiImageRenderer,
    OpenAiImageRenderError,
    UnknownModelPricingError,
    UnsupportedImageCapabilityError,
)
from tests.unit.creative.domain.factories import make_brand_kit
from tests.unit.creative.infrastructure.fakes import FakeAssetStore


def _png_bytes(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _spec(format_: Format) -> ImageSpec:
    return ImageSpec(
        prompt="escena de estudio",
        reference_assets=(),
        format=format_,
        seed=None,
        brand_kit=make_brand_kit(),
    )


def _handler_returning(image_bytes: bytes, *, usage: dict[str, object] | None):
    b64 = base64.b64encode(image_bytes).decode("ascii")
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        body: dict[str, object] = {"data": [{"b64_json": b64}]}
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    return handler, captured


def test_requests_a_multiple_of_16_size_at_least_as_large_as_the_target() -> None:
    async def _run() -> dict[str, object]:
        handler, captured = _handler_returning(
            _png_bytes(1088, 1088), usage={"output_tokens": 1000}
        )
        renderer = OpenAiImageRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="sk-test",
            asset_store=FakeAssetStore(),
        )
        await renderer.render(_spec(Format.SQUARE_1080))
        return captured["body"]  # type: ignore[return-value]

    body = asyncio.run(_run())
    assert body["size"] == "1088x1088"


def test_model_is_configurable_not_hardcoded() -> None:
    async def _run() -> dict[str, object]:
        handler, captured = _handler_returning(
            _png_bytes(1024, 1024), usage={"output_tokens": 500}
        )
        renderer = OpenAiImageRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="sk-test",
            asset_store=FakeAssetStore(),
            model="gpt-image-2",
        )
        await renderer.render(_spec(Format.SQUARE_1080))
        return captured["body"]  # type: ignore[return-value]

    body = asyncio.run(_run())
    assert body["model"] == "gpt-image-2"


def test_result_is_cropped_and_resized_to_the_exact_target_format() -> None:
    async def _run() -> tuple[int, int]:
        # El backend devuelve un tamano DISTINTO al pedido (p.ej. recorto a
        # su propio preset interno) — el resultado final debe ser igual el
        # Format exacto, porque el recorte opera sobre lo REAL devuelto.
        handler, _ = _handler_returning(_png_bytes(1024, 1536), usage={"output_tokens": 800})
        asset_store = FakeAssetStore()
        renderer = OpenAiImageRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="sk-test",
            asset_store=asset_store,
        )
        await renderer.render(_spec(Format.STORY_1080X1920))
        stored_payload = asset_store.puts[0][0]
        with Image.open(io.BytesIO(stored_payload)) as image:
            return image.width, image.height

    assert asyncio.run(_run()) == (1080, 1920)


def test_cost_is_computed_from_usage_output_tokens() -> None:
    async def _run() -> str:
        handler, _ = _handler_returning(
            _png_bytes(1088, 1088), usage={"output_tokens": 1_000_000}
        )
        renderer = OpenAiImageRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="sk-test",
            asset_store=FakeAssetStore(),
        )
        result = await renderer.render(_spec(Format.SQUARE_1080))
        return f"{result.cost_estimate.amount} {result.cost_estimate.currency}"

    assert asyncio.run(_run()) == "30.000000 USD"


def test_raises_when_response_has_no_usage() -> None:
    async def _run() -> None:
        handler, _ = _handler_returning(_png_bytes(1088, 1088), usage=None)
        renderer = OpenAiImageRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="sk-test",
            asset_store=FakeAssetStore(),
        )
        await renderer.render(_spec(Format.SQUARE_1080))

    with pytest.raises(OpenAiImageRenderError, match="usage"):
        asyncio.run(_run())


def test_raises_for_unknown_model_pricing() -> None:
    async def _run() -> None:
        handler, _ = _handler_returning(
            _png_bytes(1088, 1088), usage={"output_tokens": 100}
        )
        renderer = OpenAiImageRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="sk-test",
            asset_store=FakeAssetStore(),
            model="some-future-model",
        )
        await renderer.render(_spec(Format.SQUARE_1080))

    with pytest.raises(UnknownModelPricingError):
        asyncio.run(_run())


def test_renderer_used_is_gpt_image() -> None:
    async def _run() -> RendererName:
        handler, _ = _handler_returning(_png_bytes(1088, 1088), usage={"output_tokens": 1})
        renderer = OpenAiImageRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="sk-test",
            asset_store=FakeAssetStore(),
        )
        result = await renderer.render(_spec(Format.SQUARE_1080))
        return result.renderer_used

    assert asyncio.run(_run()) == RendererName.GPT_IMAGE_1_5


def test_capabilities_are_injectable_for_a_different_backend() -> None:
    async def _run() -> dict[str, object]:
        handler, captured = _handler_returning(
            _png_bytes(304, 256), usage={"output_tokens": 1}
        )
        tight_capabilities = ImageBackendCapabilities(
            multiple_of=16, min_total_px=0, max_total_px=10_000_000, max_side_px=3840
        )
        renderer = OpenAiImageRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="sk-test",
            asset_store=FakeAssetStore(),
            capabilities=tight_capabilities,
        )
        await renderer.render(_spec(Format.MEDIUM_RECTANGLE_300X250))
        return captured["body"]  # type: ignore[return-value]

    body = asyncio.run(_run())
    # 300->304 (siguiente multiplo de 16), 250->256, y min_total_px=0 no
    # obliga a crecer mas.
    assert body["size"] == "304x256"


def test_capability_bounds_reject_a_format_too_large_for_the_backend() -> None:
    async def _run() -> None:
        handler, _ = _handler_returning(_png_bytes(10, 10), usage={"output_tokens": 1})
        tiny_capabilities = ImageBackendCapabilities(
            multiple_of=16, min_total_px=1, max_total_px=1000, max_side_px=100
        )
        renderer = OpenAiImageRenderer(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_key="sk-test",
            asset_store=FakeAssetStore(),
            capabilities=tiny_capabilities,
        )
        await renderer.render(_spec(Format.SQUARE_1080))

    with pytest.raises(UnsupportedImageCapabilityError):
        asyncio.run(_run())
