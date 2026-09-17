"""`FalImageRenderer` implementa `ImageRendererPort` sobre la API de cola
de fal.ai — el MISMO proveedor y modelo por defecto que usa Hermes (motor
de Safent) para generar imagenes (`fal-ai/flux-2/klein/9b`, aspecto por
defecto "landscape"): paridad total de imagen entre el motor nativo y el
MCP alojado (encargo del propietario 2026-09-15,
research/content-generation-stack.md, `RendererName.FLUX2_KLEIN_9B`).

Mismo patron `submit -> poll status -> fetch result -> descargar bytes`
que `FalVideoRenderer` (`fal_adapter.py`); la clave vive en `BrokerSettings`
(threat-model.md C-29). A diferencia de `FalVideoRenderer`, todo error de
estado HTTP se traduce a `FalImageRenderError`/`FalImageTimeoutError` antes
de propagarse -- nunca un `httpx.HTTPStatusError` crudo -- para que el
mensaje nunca incluya la cabecera `Authorization`."""

from __future__ import annotations

import asyncio
import hashlib
from decimal import Decimal

import httpx

from safent_ads.creative.application.ports import AssetStorePort
from safent_ads.creative.domain.enums import MediaKind, RendererName
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError

# fal.ai no publica un precio de lista propio para `flux-2/klein/9b`
# todavia (research/content-generation-stack.md solo cita flux-2-pro:
# $0.03 el primer megapixel + $0.015/MP): 0.02 USD/imagen es una
# ESTIMACION documentada, no un precio verificado -- medir contra
# facturacion real en cuanto haya trafico (mismo criterio que
# `openai_image_adapter.UnknownModelPricingError`, pero aqui configurable
# en vez de fallar alto porque no hay ninguna respuesta de fal con un
# desglose de coste que leer).
_DEFAULT_PRICE_PER_IMAGE_USD = Decimal("0.02")
_DEFAULT_MODEL_PATH = "fal-ai/flux-2/klein/9b"
_DEFAULT_POLL_INTERVAL_S = 2.0
_NUM_IMAGES = 1
_OUTPUT_FORMAT = "jpeg"


class FalImageRenderError(InfrastructureError):
    """fal.ai devolvio un error o un resultado sin imagen."""


class FalImageTimeoutError(InfrastructureError):
    """El trabajo no termino dentro de `timeout_s`."""


def _image_size_for(spec: ImageSpec) -> dict[str, int]:
    """`ImageSpec.format` ya trae el tamano exacto de anuncio pedido
    (`Format.width`/`height`) -- fal.ai acepta un objeto `image_size`
    personalizado con esas dos claves en sus modelos flux, sin necesidad
    de recorte posterior (a diferencia de `openai_image_adapter`, que si
    lo necesita porque la Images API solo acepta un catalogo cerrado de
    tamanos)."""
    return {"width": spec.format.width, "height": spec.format.height}


def _request_body_for(spec: ImageSpec) -> dict[str, object]:
    body: dict[str, object] = {
        "prompt": spec.prompt,
        "image_size": _image_size_for(spec),
        "num_images": _NUM_IMAGES,
        "output_format": _OUTPUT_FORMAT,
        "enable_safety_checker": True,
    }
    if spec.seed is not None:
        body["seed"] = spec.seed
    return body


class FalImageRenderer:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        api_key: str,
        asset_store: AssetStorePort,
        clock: Clock,
        model_path: str = _DEFAULT_MODEL_PATH,
        price_per_image: Decimal = _DEFAULT_PRICE_PER_IMAGE_USD,
        base_url: str = "https://queue.fal.run",
        timeout_s: float = 120.0,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self.name = RendererName.FLUX2_KLEIN_9B
        self._http_client = http_client
        self._api_key = api_key
        self._asset_store = asset_store
        self._clock = clock
        self._model_path = model_path
        self._price_per_image = price_per_image
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    async def render(self, spec: ImageSpec) -> RenderedAsset:
        request_id = await self._submit(spec)
        result = await self._poll_until_done(request_id)
        payload = await self._download_image(result)
        checksum = hashlib.sha256(payload).hexdigest()
        storage_uri = await self._asset_store.put(payload, MediaKind.IMAGE)
        return RenderedAsset(
            storage_uri=storage_uri,
            media_kind=MediaKind.IMAGE,
            format=spec.format,
            checksum=checksum,
            renderer_used=self.name,
            cost_estimate=Money(self._price_per_image, "USD"),
            duration_s=None,
            generated_at=self._clock.now(),
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Key {self._api_key}"}

    def _require_success(self, response: httpx.Response) -> None:
        # Nunca `response.raise_for_status()`: su mensaje repite la URL
        # completa de la peticion y, aunque la clave viaja solo en la
        # cabecera (nunca en la URL), este adaptador construye el mensaje
        # a mano para no depender de ese detalle de httpx.
        if response.is_success:
            return
        raise FalImageRenderError(f"fal.ai respondio {response.status_code}")

    async def _submit(self, spec: ImageSpec) -> str:
        response = await self._http_client.post(
            f"{self._base_url}/{self._model_path}",
            headers=self._headers(),
            json=_request_body_for(spec),
            timeout=self._timeout_s,
        )
        self._require_success(response)
        body = response.json()
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise FalImageRenderError("respuesta sin request_id")
        return request_id

    async def _poll_until_done(self, request_id: str) -> dict[str, object]:
        status_url = f"{self._base_url}/{self._model_path}/requests/{request_id}/status"
        result_url = f"{self._base_url}/{self._model_path}/requests/{request_id}"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._timeout_s
        while True:
            status_response = await self._http_client.get(
                status_url, headers=self._headers(), timeout=self._timeout_s
            )
            self._require_success(status_response)
            status = status_response.json().get("status")
            if status == "COMPLETED":
                return await self._fetch_result(result_url)
            if status == "ERROR":
                raise FalImageRenderError("fal.ai devolvio status=ERROR")
            if loop.time() >= deadline:
                raise FalImageTimeoutError(f"{request_id} no termino en {self._timeout_s}s")
            await asyncio.sleep(self._poll_interval_s)

    async def _fetch_result(self, result_url: str) -> dict[str, object]:
        result_response = await self._http_client.get(
            result_url, headers=self._headers(), timeout=self._timeout_s
        )
        self._require_success(result_response)
        result: dict[str, object] = result_response.json()
        return result

    async def _download_image(self, result: dict[str, object]) -> bytes:
        # `url` sale de la propia respuesta de fal.ai (CDN del proveedor ya
        # configurado, no un argumento de tool ni entrada del modelo):
        # mismo criterio que `FalVideoRenderer._download_video`.
        images = result.get("images")
        if not isinstance(images, list) or not images or not isinstance(images[0], dict):
            raise FalImageRenderError("resultado sin images[0]")
        url = images[0].get("url")
        if not isinstance(url, str) or not url:
            raise FalImageRenderError("resultado sin images[0].url")
        response = await self._http_client.get(url, timeout=self._timeout_s)
        self._require_success(response)
        return response.content
