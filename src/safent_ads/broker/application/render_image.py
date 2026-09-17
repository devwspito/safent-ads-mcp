"""`RenderImageService`: unico punto donde `ads-broker` ejecuta un
renderizador de imagen de pago (Fal/OpenAI, `composition/creative_renderers
.py::build_image_renderers`) para el op `render_image` del socket
(threat-model.md C-29 "claves cloud en el broker"). Limites propios de esta
operacion, nunca los que ya aplica cada adaptador (esos protegen su propia
llamada HTTP, no la operacion completa vista desde el socket):

- **una imagen por llamada**: el esquema del wire (`request_schemas.py::
  RenderImageRequest`) no declara ningun contador -- `ImageSpec` tampoco lo
  admite.
- **tope de peticiones por minuto GLOBAL**, compartido por TODOS los
  llamadores (protege la factura del propietario en fal.ai/OpenAI frente a
  un pico agregado).
- **tope de peticiones por minuto Y por dia POR NEGOCIO** (M-2, revision de
  seguridad 0.2.22): el global de arriba no evita que UN negocio agote el
  presupuesto de todos los demas -- `ADS_RENDER_IMAGE_QUOTA_PER_MINUTE`/
  `_PER_DAY`. `business_id` es obligatorio (fail closed): sin el, el gasto
  no se puede atribuir a nadie y no hay cuota que aplicar.
- **tope de coste por llamada** (M-2): el precio de lista del renderizador
  pedido (`list_price_usd_by_renderer`, ver `composition/broker.py`) se
  compara contra `ADS_RENDER_IMAGE_MAX_COST_USD` ANTES de llamar al
  proveedor -- un renderizador sin precio conocido en la tabla falla
  cerrado (nunca se asume gratis).
- **timeout total**, que cubre el ciclo completo del adaptador (para
  `FalImageRenderer` eso es submit->poll->descarga, no solo la primera
  llamada HTTP).

Que renderizador ejecutar lo decide `ads-api` (`GenerateCreativeAssets.
_render_first_available`, cascada de `RendererSelector`) llamando una vez
por candidato -- este servicio nunca elige entre `image_renderers`, solo
valida que el pedido exista y lo ejecuta."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Protocol

from safent_ads.broker.platforms.rate_limits import WriteBudgetWindow
from safent_ads.creative.application.ports import ImageRendererPort
from safent_ads.creative.domain.enums import MediaKind, RendererName
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset
from safent_ads.creative.domain.renderer_selector import MODEL_NAME_BY_RENDERER
from safent_ads.creative.domain.storage import StorageUri
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import ApplicationError, InfrastructureError

_DEFAULT_MAX_RENDERS_PER_MINUTE = 10
_DEFAULT_RENDER_TIMEOUT_S = 150.0
_DEFAULT_QUOTA_PER_BUSINESS_PER_MINUTE = 6
_DEFAULT_QUOTA_PER_BUSINESS_PER_DAY = 120
_DEFAULT_MAX_COST_USD = Decimal("0.50")
# Red de seguridad para llamadores que no cablean su propia tabla (tests,
# sobre todo) -- `composition/broker.py` SIEMPRE pasa los valores reales de
# `BrokerSettings` (`fal_image_price_usd`/`openai_image_price_usd_estimate`)
# en produccion; estos literales son solo el mismo defecto documentado ahi,
# duplicado a proposito para no invertir la capa (`application` no importa
# `composition`).
_DEFAULT_LIST_PRICE_USD_BY_RENDERER: Mapping[RendererName, Decimal] = MappingProxyType(
    {
        RendererName.FLUX2_KLEIN_9B: Decimal("0.02"),
        RendererName.GPT_IMAGE_1_5: Decimal("0.20"),
    }
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_CONTENT_TYPE_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (_PNG_MAGIC, "image/png"),
    (_JPEG_MAGIC, "image/jpeg"),
)


class ImageRendererNotConfiguredError(ApplicationError):
    """El `RendererName` pedido no tiene adaptador inyectado en este broker
    (sin `FAL_API_KEY`/`OPENAI_API_KEY`, o nombre desconocido) -- fail
    closed. La cascada entre proveedores vive en `ads-api`
    (`GenerateCreativeAssets._render_first_available`): este servicio nunca
    intenta un segundo candidato por su cuenta."""


class ImageRenderQuotaExceededError(ApplicationError):
    """Tope de renders de imagen por minuto agotado para todo el broker."""


class RenderImageBusinessIdRequiredError(ApplicationError):
    """M-2 (revision de seguridad 0.2.22): `business_id` vacio -- fail
    closed. Sin un negocio al que atribuir el gasto no hay cuota que
    aplicar; nunca cae en un cajon compartido."""


class RenderQuotaExceededError(ApplicationError):
    """M-2: tope de renders de imagen por minuto O por dia agotado para
    ESTE negocio. Distinto de `ImageRenderQuotaExceededError` (tope
    global, compartido por todos los negocios) -- capa adicional, no la
    sustituye."""


class RenderCostCapExceededError(ApplicationError):
    """M-2: el precio de lista del renderizador pedido supera
    `max_cost_usd`, o no tiene precio conocido -- se comprueba ANTES de
    llamar al proveedor, nunca despues de gastar."""


class ImageRenderTimeoutError(InfrastructureError):
    """El adaptador no completo el ciclo submit->poll->descarga dentro de
    `render_timeout_s`."""


class UnrecognizedImagePayloadError(InfrastructureError):
    """El adaptador devolvio bytes que no son ni PNG ni JPEG -- ningun
    `ImageRendererPort` registrado hoy produce otro contenedor; fail loud
    en vez de adivinar un `content_type`."""


class _PerBusinessBudget:
    """Una `WriteBudgetWindow` propia por negocio, creada perezosamente al
    primer uso. El tope global (`WriteBudgetWindow` compartido de
    `RenderImageService`) protege la factura del propietario frente a un
    pico agregado; este protege a un negocio de acaparar ese tope
    compartido a costa de los demas."""

    def __init__(self, max_calls: int, window: timedelta, clock: Clock) -> None:
        self._max_calls = max_calls
        self._window = window
        self._clock = clock
        self._windows: dict[str, WriteBudgetWindow] = {}

    def try_consume(self, business_id: str) -> bool:
        window = self._windows.get(business_id)
        if window is None:
            window = WriteBudgetWindow(self._max_calls, self._window, self._clock)
            self._windows[business_id] = window
        return window.try_consume()


class RenderImageAssetStore(Protocol):
    """Puerto propio de `application` (guard arquitectonica: `application`
    nunca importa `infrastructure`, ni siquiera para un type hint):
    subconjunto de `creative.application.ports.AssetStorePort` mas `pop`
    (recuperar-y-descartar). `InMemoryAssetStore`
    (`creative/infrastructure/in_memory_asset_store.py`) lo implementa de
    sobra; `composition/broker.py` es quien la construye y la inyecta
    aqui -- este modulo nunca conoce el tipo concreto."""

    async def put(self, payload: bytes, media_kind: MediaKind) -> StorageUri: ...

    def pop(self, uri: StorageUri) -> bytes: ...


@dataclass(frozen=True, slots=True)
class RenderImageResult:
    rendered: RenderedAsset
    payload: bytes
    content_type: str
    model_name: str


def _sniff_image_content_type(payload: bytes) -> str:
    for magic, content_type in _CONTENT_TYPE_BY_MAGIC:
        if payload.startswith(magic):
            return content_type
    raise UnrecognizedImagePayloadError("payload no es PNG ni JPEG")


class RenderImageService:
    def __init__(
        self,
        image_renderers: Mapping[RendererName, ImageRendererPort],
        *,
        asset_store: RenderImageAssetStore,
        clock: Clock,
        max_renders_per_minute: int = _DEFAULT_MAX_RENDERS_PER_MINUTE,
        render_timeout_s: float = _DEFAULT_RENDER_TIMEOUT_S,
        quota_per_business_per_minute: int = _DEFAULT_QUOTA_PER_BUSINESS_PER_MINUTE,
        quota_per_business_per_day: int = _DEFAULT_QUOTA_PER_BUSINESS_PER_DAY,
        max_cost_usd: Decimal = _DEFAULT_MAX_COST_USD,
        list_price_usd_by_renderer: Mapping[
            RendererName, Decimal
        ] = _DEFAULT_LIST_PRICE_USD_BY_RENDERER,
    ) -> None:
        self._image_renderers = image_renderers
        self._asset_store = asset_store
        self._render_timeout_s = render_timeout_s
        self._quota = WriteBudgetWindow(
            max_renders_per_minute, timedelta(minutes=1), clock
        )
        self._quota_per_business_minute = _PerBusinessBudget(
            quota_per_business_per_minute, timedelta(minutes=1), clock
        )
        self._quota_per_business_day = _PerBusinessBudget(
            quota_per_business_per_day, timedelta(days=1), clock
        )
        self._max_cost_usd = max_cost_usd
        self._list_price_usd_by_renderer = list_price_usd_by_renderer

    async def render(
        self, *, renderer: RendererName, spec: ImageSpec, business_id: str
    ) -> RenderImageResult:
        if not business_id:
            raise RenderImageBusinessIdRequiredError("business_id_required")
        if not self._quota.try_consume():
            raise ImageRenderQuotaExceededError("render_image_quota_exceeded")
        if not self._quota_per_business_minute.try_consume(business_id):
            raise RenderQuotaExceededError("render_image_quota_exceeded_per_minute")
        if not self._quota_per_business_day.try_consume(business_id):
            raise RenderQuotaExceededError("render_image_quota_exceeded_per_day")
        adapter = self._image_renderers.get(renderer)
        if adapter is None:
            raise ImageRendererNotConfiguredError(renderer.value)
        self._require_within_cost_cap(renderer)
        rendered = await self._render_with_timeout(adapter, spec)
        payload = self._asset_store.pop(rendered.storage_uri)
        return RenderImageResult(
            rendered=rendered,
            payload=payload,
            content_type=_sniff_image_content_type(payload),
            model_name=MODEL_NAME_BY_RENDERER[rendered.renderer_used],
        )

    def _require_within_cost_cap(self, renderer: RendererName) -> None:
        list_price = self._list_price_usd_by_renderer.get(renderer)
        if list_price is None or list_price > self._max_cost_usd:
            raise RenderCostCapExceededError(
                f"{renderer.value}: precio de lista {list_price} supera "
                f"el tope {self._max_cost_usd} USD"
            )

    async def _render_with_timeout(
        self, adapter: ImageRendererPort, spec: ImageSpec
    ) -> RenderedAsset:
        try:
            return await asyncio.wait_for(adapter.render(spec), timeout=self._render_timeout_s)
        except TimeoutError as exc:
            raise ImageRenderTimeoutError(f"{self._render_timeout_s}s agotados") from exc
