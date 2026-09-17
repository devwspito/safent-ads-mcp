"""`FalImageRenderer` contra un servidor HTTP falso (`httpx.MockTransport`,
sin red real): submit -> poll -> fetch result -> descarga de imagen, mismo
patron que `test_fal_video_renderer_polls_until_completed`
(`test_cloud_and_voice_adapters.py`). Cubre ademas lo que `FalVideoRenderer`
no cubre: un estado de error de fal.ai se traduce a un tipo propio sin la
clave en el mensaje (nunca `httpx.HTTPStatusError` crudo)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from safent_ads.creative.application.ports import AssetStorePort
from safent_ads.creative.domain.enums import Format, RendererName
from safent_ads.creative.domain.render_specs import ImageSpec
from safent_ads.creative.infrastructure.fal_image_adapter import (
    FalImageRenderer,
    FalImageRenderError,
    FalImageTimeoutError,
)
from safent_ads.shared.clock import FixedClock
from tests.unit.creative.domain.factories import make_brand_kit
from tests.unit.creative.infrastructure.fakes import FakeAssetStore

_API_KEY = "fal-super-secret-key-0123456789"
_FIXED_CLOCK = FixedClock(fixed_at=datetime(2026, 9, 15, tzinfo=UTC))


def _spec(*, seed: int | None = 7, format_: Format = Format.SQUARE_1080) -> ImageSpec:
    return ImageSpec(
        prompt="perro en un parque, luz natural",
        reference_assets=(),
        format=format_,
        seed=seed,
        brand_kit=make_brand_kit(),
    )


def _renderer(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    asset_store: AssetStorePort | None = None,
    model_path: str = "fal-ai/flux-2/klein/9b",
    price_per_image: Decimal = Decimal("0.02"),
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.01,
) -> FalImageRenderer:
    return FalImageRenderer(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        api_key=_API_KEY,
        asset_store=asset_store if asset_store is not None else FakeAssetStore(),
        clock=_FIXED_CLOCK,
        model_path=model_path,
        price_per_image=price_per_image,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
    )


def _happy_path_handler(*, model_path: str = "flux-2/klein/9b", completes_after: int = 2):
    """`request_id="req-img-1"` completa tras `completes_after` sondeos y
    devuelve una unica imagen -- mismo patron de contador que
    `test_fal_video_renderer_polls_until_completed`."""
    status_calls = {"count": 0}
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith(model_path):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"request_id": "req-img-1"})
        if path.endswith("/status"):
            status_calls["count"] += 1
            status = "COMPLETED" if status_calls["count"] >= completes_after else "IN_PROGRESS"
            return httpx.Response(200, json={"status": status})
        if path.endswith("/requests/req-img-1"):
            return httpx.Response(200, json={"images": [{"url": "https://fal.media/out.jpg"}]})
        if path == "/out.jpg":
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0fake-jpeg-bytes")
        raise AssertionError(f"unexpected request {request.method} {path}")

    return handler, captured


def test_renders_stores_the_image_and_returns_model_and_cost() -> None:
    async def _run() -> tuple[object, dict[str, object], FakeAssetStore]:
        handler, captured = _happy_path_handler()
        asset_store = FakeAssetStore()
        renderer = _renderer(handler, asset_store=asset_store, price_per_image=Decimal("0.03"))
        result = await renderer.render(_spec())
        return result, captured, asset_store

    result, captured, asset_store = asyncio.run(_run())

    assert result.renderer_used == RendererName.FLUX2_KLEIN_9B
    assert result.generated_at == _FIXED_CLOCK.now()
    assert f"{result.cost_estimate.amount} {result.cost_estimate.currency}" == "0.03 USD"
    assert len(asset_store.puts) == 1
    body = captured["body"]
    assert body["image_size"] == {"width": 1080, "height": 1080}
    assert body["seed"] == 7
    assert body["num_images"] == 1
    assert body["enable_safety_checker"] is True


def test_model_path_is_configurable_not_hardcoded() -> None:
    """La ruta del modelo se usa DE VERDAD en las tres URLs (submit, status,
    result) -- el handler falso levanta `AssertionError` para cualquier
    peticion que no encaje con `model_path`, asi que esto falla si el
    renderer sigue enviando el modelo por defecto."""

    async def _run() -> dict[str, object]:
        handler, captured = _happy_path_handler(model_path="fal-ai/some-other-model")
        renderer = _renderer(handler, model_path="fal-ai/some-other-model")
        await renderer.render(_spec())
        return captured["body"]  # type: ignore[return-value]

    body = asyncio.run(_run())
    assert body["prompt"] == "perro en un parque, luz natural"


def test_image_size_maps_the_requested_ad_format() -> None:
    async def _run() -> dict[str, object]:
        handler, captured = _happy_path_handler()
        renderer = _renderer(handler)
        await renderer.render(_spec(format_=Format.STORY_1080X1920))
        return captured["body"]  # type: ignore[return-value]

    body = asyncio.run(_run())
    assert body["image_size"] == {"width": 1080, "height": 1920}


def test_seed_is_omitted_from_the_request_when_the_spec_has_none() -> None:
    async def _run() -> dict[str, object]:
        handler, captured = _happy_path_handler()
        renderer = _renderer(handler)
        await renderer.render(_spec(seed=None))
        return captured["body"]  # type: ignore[return-value]

    body = asyncio.run(_run())
    assert "seed" not in body


def test_times_out_when_the_job_never_completes() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"request_id": "req-1"})
            return httpx.Response(200, json={"status": "IN_PROGRESS"})

        renderer = _renderer(handler, timeout_s=0.05, poll_interval_s=0.01)
        await renderer.render(_spec())

    with pytest.raises(FalImageTimeoutError):
        asyncio.run(_run())


def test_fal_status_error_raises_error_reports_error_status() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"request_id": "req-1"})
            return httpx.Response(200, json={"status": "ERROR"})

        renderer = _renderer(handler, poll_interval_s=0.01)
        await renderer.render(_spec())

    with pytest.raises(FalImageRenderError, match="ERROR"):
        asyncio.run(_run())


def test_http_error_status_raises_typed_error_without_leaking_the_key() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(401, json={"detail": "invalid key"})

        renderer = _renderer(handler)
        await renderer.render(_spec())

    with pytest.raises(FalImageRenderError) as excinfo:
        asyncio.run(_run())
    assert _API_KEY not in str(excinfo.value)


def test_http_error_status_while_polling_raises_typed_error() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"request_id": "req-1"})
            return httpx.Response(500, json={"detail": "boom"})

        renderer = _renderer(handler, poll_interval_s=0.01)
        await renderer.render(_spec())

    with pytest.raises(FalImageRenderError) as excinfo:
        asyncio.run(_run())
    assert _API_KEY not in str(excinfo.value)


def test_result_without_images_raises_typed_error() -> None:
    async def _run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"request_id": "req-1"})
            if request.url.path.endswith("/status"):
                return httpx.Response(200, json={"status": "COMPLETED"})
            return httpx.Response(200, json={"images": []})

        renderer = _renderer(handler)
        await renderer.render(_spec())

    with pytest.raises(FalImageRenderError, match="images"):
        asyncio.run(_run())
