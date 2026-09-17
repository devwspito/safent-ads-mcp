"""`RenderImageService` (broker/application/render_image.py, lane 003):
limites propios de `render_image` -- renderizador no configurado, cuota
por minuto y timeout total del ciclo -- nunca los del adaptador mismo (un
doble simple basta, la logica de fal.ai/OpenAI ya se prueba en
`tests/unit/creative/infrastructure/`)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from safent_ads.broker.application.render_image import (
    ImageRendererNotConfiguredError,
    ImageRenderQuotaExceededError,
    ImageRenderTimeoutError,
    RenderCostCapExceededError,
    RenderImageBusinessIdRequiredError,
    RenderImageService,
    RenderQuotaExceededError,
    UnrecognizedImagePayloadError,
    _sniff_image_content_type,
)
from safent_ads.creative.domain.enums import Format, MediaKind, RendererName
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset
from safent_ads.creative.infrastructure.in_memory_asset_store import InMemoryAssetStore
from safent_ads.shared.clock import FixedClock
from tests.unit.creative.domain.factories import make_brand_kit

_NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"
_JPEG_BYTES = b"\xff\xd8\xff" + b"fake-jpeg-body"


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


class _FakeImageRenderer:
    def __init__(
        self,
        name: RendererName,
        payload: bytes,
        asset_store: InMemoryAssetStore,
        *,
        never_completes: bool = False,
    ) -> None:
        self.name = name
        self._payload = payload
        self._asset_store = asset_store
        self._never_completes = never_completes

    async def render(self, spec: ImageSpec) -> RenderedAsset:
        if self._never_completes:
            await asyncio.sleep(3600)
        storage_uri = await self._asset_store.put(self._payload, MediaKind.IMAGE)
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


def _service(
    *,
    payload: bytes = _PNG_BYTES,
    renderer_name: RendererName = RendererName.FLUX2_KLEIN_9B,
    never_completes: bool = False,
    max_renders_per_minute: int = 10,
    render_timeout_s: float = 150.0,
    quota_per_business_per_minute: int = 6,
    quota_per_business_per_day: int = 120,
    max_cost_usd: Decimal = Decimal("0.50"),
    list_price_usd_by_renderer: dict[RendererName, Decimal] | None = None,
) -> tuple[RenderImageService, InMemoryAssetStore]:
    asset_store = InMemoryAssetStore()
    renderer = _FakeImageRenderer(
        renderer_name, payload, asset_store, never_completes=never_completes
    )
    kwargs: dict[str, object] = {}
    if list_price_usd_by_renderer is not None:
        kwargs["list_price_usd_by_renderer"] = list_price_usd_by_renderer
    service = RenderImageService(
        {renderer_name: renderer},
        asset_store=asset_store,
        clock=FixedClock(_NOW),
        max_renders_per_minute=max_renders_per_minute,
        render_timeout_s=render_timeout_s,
        quota_per_business_per_minute=quota_per_business_per_minute,
        quota_per_business_per_day=quota_per_business_per_day,
        max_cost_usd=max_cost_usd,
        **kwargs,
    )
    return service, asset_store


async def test_render_returns_the_decoded_payload_and_sniffed_content_type() -> None:
    service, _ = _service(payload=_PNG_BYTES)

    result = await service.render(
        renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1"
    )

    assert result.payload == _PNG_BYTES
    assert result.content_type == "image/png"
    assert result.model_name == "flux-2-klein-9b"
    assert result.rendered.renderer_used == RendererName.FLUX2_KLEIN_9B


async def test_render_sniffs_jpeg_content_type() -> None:
    service, _ = _service(payload=_JPEG_BYTES)

    result = await service.render(
        renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1"
    )

    assert result.content_type == "image/jpeg"


async def test_render_pops_the_payload_from_the_asset_store() -> None:
    """El almacen en memoria nunca retiene un activo mas alla de la
    peticion que lo produjo: un segundo `pop` de la misma clave debe
    fallar."""
    service, asset_store = _service()

    result = await service.render(
        renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1"
    )

    with pytest.raises(Exception):  # noqa: B017, PT011 - UnknownStorageUriError, ya consumida
        asset_store.pop(result.rendered.storage_uri)


async def test_render_raises_when_the_renderer_is_not_configured() -> None:
    service, _ = _service(renderer_name=RendererName.FLUX2_KLEIN_9B)

    with pytest.raises(ImageRendererNotConfiguredError):
        await service.render(renderer=RendererName.GPT_IMAGE_1_5, spec=_spec(), business_id="biz-1")


async def test_render_raises_when_the_per_minute_quota_is_exhausted() -> None:
    service, _ = _service(max_renders_per_minute=1)

    await service.render(renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1")
    with pytest.raises(ImageRenderQuotaExceededError):
        await service.render(
            renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-2"
        )


async def test_render_raises_timeout_when_the_adapter_never_completes() -> None:
    service, _ = _service(never_completes=True, render_timeout_s=0.01)

    with pytest.raises(ImageRenderTimeoutError):
        await service.render(
            renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1"
        )


def test_sniff_image_content_type_rejects_an_unrecognized_payload() -> None:
    with pytest.raises(UnrecognizedImagePayloadError):
        _sniff_image_content_type(b"not-an-image")


# ---------------------------------------------------------------------------
# M-2 (revision de seguridad 0.2.22): cuota por negocio (minuto y dia),
# tope de coste antes de llamar al proveedor, fail-closed sin business_id.
# ---------------------------------------------------------------------------


async def test_render_requires_a_non_empty_business_id() -> None:
    service, _ = _service()

    with pytest.raises(RenderImageBusinessIdRequiredError):
        await service.render(renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="")


async def test_render_raises_when_a_single_business_exhausts_its_own_per_minute_quota() -> None:
    service, _ = _service(quota_per_business_per_minute=1)

    await service.render(renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1")
    with pytest.raises(RenderQuotaExceededError):
        await service.render(
            renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1"
        )


async def test_render_per_minute_quota_is_isolated_per_business() -> None:
    """El tope por negocio nunca se comparte entre negocios distintos --
    justo la brecha que M-2 cierra frente al tope global compartido."""
    service, _ = _service(quota_per_business_per_minute=1)

    await service.render(renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1")
    result = await service.render(
        renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-2"
    )

    assert result.rendered.renderer_used == RendererName.FLUX2_KLEIN_9B


async def test_render_raises_when_a_single_business_exhausts_its_own_per_day_quota() -> None:
    service, _ = _service(quota_per_business_per_minute=100, quota_per_business_per_day=1)

    await service.render(renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1")
    with pytest.raises(RenderQuotaExceededError):
        await service.render(
            renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1"
        )


async def test_render_raises_when_the_list_price_exceeds_the_cost_cap() -> None:
    service, _ = _service(
        max_cost_usd=Decimal("0.10"),
        list_price_usd_by_renderer={RendererName.FLUX2_KLEIN_9B: Decimal("0.20")},
    )

    with pytest.raises(RenderCostCapExceededError):
        await service.render(
            renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1"
        )


async def test_render_raises_when_the_renderer_has_no_known_price() -> None:
    """Fail closed: un renderizador sin precio en la tabla nunca se asume
    gratis."""
    service, _ = _service(max_cost_usd=Decimal("999"), list_price_usd_by_renderer={})

    with pytest.raises(RenderCostCapExceededError):
        await service.render(
            renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1"
        )


async def test_render_succeeds_when_the_list_price_is_within_the_cost_cap() -> None:
    service, _ = _service(
        max_cost_usd=Decimal("0.50"),
        list_price_usd_by_renderer={RendererName.FLUX2_KLEIN_9B: Decimal("0.02")},
    )

    result = await service.render(
        renderer=RendererName.FLUX2_KLEIN_9B, spec=_spec(), business_id="biz-1"
    )

    assert result.rendered.renderer_used == RendererName.FLUX2_KLEIN_9B
