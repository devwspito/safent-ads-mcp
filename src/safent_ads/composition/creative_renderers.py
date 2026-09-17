"""`build_image_renderers`: unica factoria de los adaptadores
`ImageRendererPort` de proveedor directo (BYOK) que `GenerateCreativeAssets`
puede seleccionar via `RendererSelector` (`creative/domain/renderer_selector.py`
`RendererTier.DIRECT_PROVIDER`).

Sin clave -> el proveedor esta ausente del `dict` devuelto (cierre en falso,
mismo criterio que `build_registry(local_enabled=False)`); con clave -> se
instancia sobre la MISMA `AssetStorePort` que ya sirve `/creatives`/previews
(`composition/app.py::_build_creative_asset_store`), para que un activo
generado por cualquiera de los dos proveedores viva en el mismo almacen
trazable.

Encargo del propietario 2026-09-15 ("total parity" entre el motor nativo de
Safent y el MCP alojado): `FalImageRenderer` (fal.ai `flux-2/klein/9b`, el
mismo proveedor+modelo que ya usa Hermes) es el proveedor por defecto;
`OpenAiImageRenderer` sigue disponible como alternativa cuando el
propietario configura `OPENAI_API_KEY` en su lugar o ademas -- los dos
pueden convivir en el registro a la vez, `RendererSelector` decide el
orden."""

from __future__ import annotations

import httpx

from safent_ads.composition.settings import BrokerSettings
from safent_ads.creative.application.ports import AssetStorePort, ImageRendererPort
from safent_ads.creative.domain.enums import RendererName
from safent_ads.creative.infrastructure.fal_image_adapter import FalImageRenderer
from safent_ads.creative.infrastructure.openai_image_adapter import OpenAiImageRenderer
from safent_ads.shared.clock import Clock


def build_image_renderers(
    settings: BrokerSettings, *, asset_store: AssetStorePort, clock: Clock
) -> dict[RendererName, ImageRendererPort]:
    renderers: dict[RendererName, ImageRendererPort] = {}
    _add_fal_renderer(renderers, settings, asset_store=asset_store, clock=clock)
    _add_openai_renderer(renderers, settings, asset_store=asset_store)
    return renderers


def _add_fal_renderer(
    renderers: dict[RendererName, ImageRendererPort],
    settings: BrokerSettings,
    *,
    asset_store: AssetStorePort,
    clock: Clock,
) -> None:
    if settings.fal_api_key is None:
        return
    renderer = FalImageRenderer(
        httpx.AsyncClient(),
        api_key=settings.fal_api_key.get_secret_value(),
        asset_store=asset_store,
        clock=clock,
        model_path=settings.fal_image_model,
        price_per_image=settings.fal_image_price_usd,
    )
    renderers[renderer.name] = renderer


def _add_openai_renderer(
    renderers: dict[RendererName, ImageRendererPort],
    settings: BrokerSettings,
    *,
    asset_store: AssetStorePort,
) -> None:
    if settings.openai_api_key is None:
        return
    renderer = OpenAiImageRenderer(
        httpx.AsyncClient(),
        api_key=settings.openai_api_key.get_secret_value(),
        asset_store=asset_store,
    )
    renderers[renderer.name] = renderer
