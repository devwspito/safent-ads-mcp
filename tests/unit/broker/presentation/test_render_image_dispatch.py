"""`render_image` (lane 003) enrutado de extremo a extremo -- `handle_payload()`
con un `RenderImageService` real sobre un `ImageRendererPort` doble, mismo
criterio que `test_reference_and_graph_ops_dispatch.py`: ningun handler
nuevo se prueba solo contra el servicio de aplicacion, se prueba contra el
sobre JSON completo, como lo vera `ads-api` de verdad."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from safent_ads.broker.application.render_image import RenderImageService
from safent_ads.broker.presentation.dispatcher import BrokerRuntime, handle_payload
from safent_ads.creative.domain.enums import MediaKind, RendererName
from safent_ads.creative.domain.identifiers import AssetId
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset
from safent_ads.creative.infrastructure.in_memory_asset_store import InMemoryAssetStore
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png-body"

_BRAND_KIT_FIELDS = {
    "primary_font": "Inter",
    "secondary_font": "Inter",
    "primary_color_hex": "#112233",
    "secondary_color_hex": "#FFFFFF",
    "logo_asset_id": str(AssetId.new()),
    "safe_area_top": 0.1,
    "safe_area_bottom": 0.1,
    "safe_area_left": 0.05,
    "safe_area_right": 0.05,
}


def _payload(**overrides: object) -> bytes:
    body: dict[str, object] = {
        "op": "render_image",
        "business_id": "biz-1",
        "renderer": RendererName.FLUX2_KLEIN_9B.value,
        "prompt": "un perro feliz en un parque",
        "format": "1080x1080",
        "seed": 7,
        "brand_kit": _BRAND_KIT_FIELDS,
    }
    body.update(overrides)
    return json.dumps(body).encode()


class _FakeImageRenderer:
    def __init__(self, name: RendererName, asset_store: InMemoryAssetStore) -> None:
        self.name = name
        self._asset_store = asset_store

    async def render(self, spec: ImageSpec) -> RenderedAsset:
        storage_uri = await self._asset_store.put(_PNG_BYTES, MediaKind.IMAGE)
        return RenderedAsset(
            storage_uri=storage_uri,
            media_kind=MediaKind.IMAGE,
            format=spec.format,
            checksum="deadbeef",
            renderer_used=self.name,
            cost_estimate=Money(Decimal("0.02"), "USD"),
            duration_s=1.5,
            generated_at=_NOW,
        )


def _runtime(*, render_image_service: RenderImageService | None) -> BrokerRuntime:
    return BrokerRuntime(
        adapters=SimpleNamespace(),  # type: ignore[arg-type] - unused by render_image
        oauth_flow=SimpleNamespace(),  # type: ignore[arg-type]
        app_credentials=SimpleNamespace(),  # type: ignore[arg-type]
        render_image_service=render_image_service,
    )


def _service_with(renderer_name: RendererName = RendererName.FLUX2_KLEIN_9B) -> RenderImageService:
    asset_store = InMemoryAssetStore()
    renderer = _FakeImageRenderer(renderer_name, asset_store)
    return RenderImageService(
        {renderer_name: renderer}, asset_store=asset_store, clock=FixedClock(_NOW)
    )


async def test_render_image_end_to_end() -> None:
    runtime = _runtime(render_image_service=_service_with())

    response = json.loads(await handle_payload(_payload(), runtime))

    assert response["ok"] is True
    result = response["result"]
    assert base64.b64decode(result["image_base64"]) == _PNG_BYTES
    assert result["content_type"] == "image/png"
    assert result["model_name"] == "flux-2-klein-9b"
    assert result["cost"] == {"amount": "0.02", "currency": "USD"}
    assert result["provenance"]["renderer"] == "flux2_klein_9b"
    assert result["provenance"]["checksum"] == "deadbeef"
    assert result["provenance"]["duration_s"] == 1.5


async def test_render_image_denies_an_unconfigured_renderer() -> None:
    runtime = _runtime(render_image_service=_service_with(RendererName.FLUX2_KLEIN_9B))

    response = json.loads(
        await handle_payload(_payload(renderer=RendererName.GPT_IMAGE_1_5.value), runtime)
    )

    assert response == {
        "ok": False,
        "error_code": "IMAGE_RENDERER_NOT_CONFIGURED",
        "reason": "denied",
    }


async def test_render_image_denies_an_unknown_renderer_name() -> None:
    runtime = _runtime(render_image_service=_service_with())
    payload = _payload(renderer="totally_bogus_renderer")

    response = json.loads(await handle_payload(payload, runtime))

    assert response == {
        "ok": False,
        "error_code": "IMAGE_RENDERER_NOT_CONFIGURED",
        "reason": "denied",
    }


async def test_render_image_denies_when_no_service_is_wired() -> None:
    runtime = _runtime(render_image_service=None)

    response = json.loads(await handle_payload(_payload(), runtime))

    assert response == {
        "ok": False,
        "error_code": "IMAGE_RENDERER_NOT_CONFIGURED",
        "reason": "denied",
    }


async def test_render_image_rejects_a_request_missing_brand_kit() -> None:
    runtime = _runtime(render_image_service=_service_with())
    body = json.loads(_payload())
    del body["brand_kit"]

    response = json.loads(await handle_payload(json.dumps(body).encode(), runtime))

    assert response == {"ok": False, "error_code": "DENIED", "reason": "invalid_schema"}


async def test_render_image_rejects_a_missing_business_id() -> None:
    """M-2 (revision de seguridad 0.2.22): `business_id` es obligatorio --
    sin el, `RenderImageService` no tiene a quien aplicar la cuota, asi
    que el esquema lo rechaza antes de que el servicio llegue a verlo."""
    runtime = _runtime(render_image_service=_service_with())
    body = json.loads(_payload())
    del body["business_id"]

    response = json.loads(await handle_payload(json.dumps(body).encode(), runtime))

    assert response == {"ok": False, "error_code": "DENIED", "reason": "invalid_schema"}


async def test_render_image_rejects_an_empty_business_id() -> None:
    runtime = _runtime(render_image_service=_service_with())

    response = json.loads(await handle_payload(_payload(business_id=""), runtime))

    assert response == {"ok": False, "error_code": "DENIED", "reason": "invalid_schema"}


async def test_render_image_rejects_a_prompt_over_the_length_cap() -> None:
    runtime = _runtime(render_image_service=_service_with())

    response = json.loads(await handle_payload(_payload(prompt="x" * 2001), runtime))

    assert response == {"ok": False, "error_code": "DENIED", "reason": "invalid_schema"}


async def test_render_image_never_echoes_the_prompt_back_in_the_response() -> None:
    """threat-model.md C-29: el sobre `ok:false`/`ok:true` nunca lleva el
    prompt de vuelta -- solo cruzan la respuesta los campos que
    `serialize_rendered_image` decide (renderizador, coste, provenance)."""
    runtime = _runtime(render_image_service=_service_with())
    sensitive_prompt_text = "unicorn-prompt-should-never-leak-anywhere"

    response = json.loads(await handle_payload(_payload(prompt=sensitive_prompt_text), runtime))

    assert sensitive_prompt_text not in json.dumps(response)
