"""`OpenAiImageRenderer` implementa `ImageRendererPort` sobre la Images API
de OpenAI — un proveedor directo (BYOK) entre varios posibles
(`RendererTier.DIRECT_PROVIDER`, `renderer_selector.py`); solo se
selecciona si el propietario configuro `OPENAI_API_KEY`. La clave vive en
`BrokerSettings` (threat-model.md C-29): este adaptador solo recibe el
valor ya resuelto, nunca lee el entorno por si mismo.

**Correcciones 2026-09-09** (research/creative-via-codex.md §(c) punto 2,
mas la correccion final del propietario "keep the crop/scale math generic,
driven by whatever the backend returns"):

1. **`model` es configuracion, no una constante.** El precio de lista
   varia por modelo (`_OUTPUT_TOKEN_PRICE_USD_PER_MILLION`); pedir uno sin
   precio conocido falla alto en vez de inventar un coste.
2. **El tamano pedido es calculado, no una tabla fija por `Format`.**
   `ImageBackendCapabilities` declara las reglas de la Images API
   (multiplo de 16, pixeles totales minimos/maximos, lado maximo) como
   datos inyectables — un backend distinto es otra instancia, no una rama
   nueva de codigo. `_request_size_for` calcula el menor tamano valido que
   no obliga a downscale (nunca amplia la imagen final).
3. **El recorte a la relacion de aspecto exacta es generico
   (`domain/image_crop.py`) sobre las dimensiones REALES devueltas**, no
   sobre lo que se pidio: si el backend no respeta el tamano exacto
   solicitado (recorte a un preset propio, por ejemplo), el resultado
   sigue siendo el `Format` correcto.
4. **El coste se lee de `usage` en la respuesta.** Sin `usage` no hay
   coste real que registrar: falla alto en vez de fingir gratis
   (`_GPT_IMAGE_COST = Money(Decimal("0.04"))` inventado, retirado)."""

from __future__ import annotations

import base64
import hashlib
import io
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from PIL import Image

from safent_ads.creative.application.ports import AssetStorePort
from safent_ads.creative.domain.enums import MediaKind, RendererName
from safent_ads.creative.domain.image_crop import crop_to_format
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset
from safent_ads.shared.errors import InfrastructureError

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-image-2.5-flare"


# developers.openai.com/api/docs/guides/image-generation (research/creative-via-codex.md
# §(a) tabla "Tamanos de imagen arbitrarios"): multiplo de 16, ratio 1:3-3:1
# (ya garantizado por Format.is_renderer_eligible), 655.360-8.294.400 px
# totales, <=3840 px por lado.
@dataclass(frozen=True, slots=True)
class ImageBackendCapabilities:
    multiple_of: int = 16
    min_total_px: int = 655_360
    max_total_px: int = 8_294_400
    max_side_px: int = 3840


_DEFAULT_CAPABILITIES = ImageBackendCapabilities()


# developers.openai.com/api/docs/pricing, research/creative-via-codex.md §(a):
# precio de lista por token de SALIDA. Sin precio de entrada fiable para
# todos los modelos todavia — se mide cuando haya trafico real.
_OUTPUT_TOKEN_PRICE_USD_PER_MILLION: dict[str, Decimal] = {
    "gpt-image-2.5-flare": Decimal("30.00"),
    "gpt-image-2.5-sunburst": Decimal("30.00"),
    "gpt-image-2": Decimal("30.00"),
    "gpt-image-1.5": Decimal("32.00"),
}


class OpenAiImageRenderError(InfrastructureError):
    """La Images API devolvio un error o una respuesta sin `b64_json`."""


class UnknownModelPricingError(InfrastructureError):
    """`model` no tiene precio de lista conocido: no se inventa un coste
    (research/creative-via-codex.md §(a): "medir empiricamente... antes de
    fijar Money en el dominio"). Añadelo a
    `_OUTPUT_TOKEN_PRICE_USD_PER_MILLION` antes de usar este modelo."""


class UnsupportedImageCapabilityError(InfrastructureError):
    """El `Format` pedido no cabe dentro de `ImageBackendCapabilities` sin
    superar `max_side_px`/`max_total_px` — configuracion incompatible, no
    un fallo transitorio."""


def _round_up_to_multiple(value: int, multiple: int) -> int:
    return -(-value // multiple) * multiple


def _grow_to_minimum_total_px(
    width: int, height: int, capabilities: ImageBackendCapabilities
) -> tuple[int, int]:
    total = width * height
    if total >= capabilities.min_total_px:
        return width, height
    scale = math.ceil(math.sqrt(capabilities.min_total_px / total))
    return (
        _round_up_to_multiple(width * scale, capabilities.multiple_of),
        _round_up_to_multiple(height * scale, capabilities.multiple_of),
    )


def _require_within_bounds(
    width: int, height: int, capabilities: ImageBackendCapabilities
) -> None:
    if max(width, height) > capabilities.max_side_px or width * height > capabilities.max_total_px:
        raise UnsupportedImageCapabilityError(
            f"{width}x{height} supera los limites del backend ({capabilities})"
        )


def _request_size_for(
    format_width: int, format_height: int, capabilities: ImageBackendCapabilities
) -> tuple[int, int]:
    """Menor tamano, multiplo de `capabilities.multiple_of` en ambos ejes,
    que no obliga a ampliar la imagen final (ancho/alto >= el tamano de
    anuncio pedido) y respeta los limites de pixeles totales del backend.
    No depende de la relacion de aspecto exacta: `crop_to_format` normaliza
    lo que sea que devuelva el backend."""
    width = _round_up_to_multiple(format_width, capabilities.multiple_of)
    height = _round_up_to_multiple(format_height, capabilities.multiple_of)
    width, height = _grow_to_minimum_total_px(width, height, capabilities)
    _require_within_bounds(width, height, capabilities)
    return width, height


def _price_per_million_output_tokens(model: str) -> Decimal:
    price = _OUTPUT_TOKEN_PRICE_USD_PER_MILLION.get(model)
    if price is None:
        raise UnknownModelPricingError(model)
    return price


def _cost_from_usage(model: str, body: dict[str, object]) -> Money:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        raise OpenAiImageRenderError(f"respuesta sin 'usage', no se puede medir el coste: {body!r}")
    output_tokens = usage.get("output_tokens")
    if not isinstance(output_tokens, int):
        raise OpenAiImageRenderError(f"'usage' sin output_tokens entero: {usage!r}")
    price = _price_per_million_output_tokens(model)
    amount = (Decimal(output_tokens) * price / Decimal(1_000_000)).quantize(Decimal("0.000001"))
    return Money(amount, "USD")


class OpenAiImageRenderer:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        api_key: str,
        asset_store: AssetStorePort,
        model: str = _DEFAULT_MODEL,
        capabilities: ImageBackendCapabilities = _DEFAULT_CAPABILITIES,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_s: float = 90.0,
    ) -> None:
        self.name = RendererName.GPT_IMAGE_1_5
        self._http_client = http_client
        self._api_key = api_key
        self._asset_store = asset_store
        self._model = model
        self._capabilities = capabilities
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def render(self, spec: ImageSpec) -> RenderedAsset:
        width, height = _request_size_for(spec.format.width, spec.format.height, self._capabilities)
        body = await self._generate(spec.prompt, width, height)
        raw_payload = self._decode_first_image(body)
        payload = self._crop_and_resize(raw_payload, spec)
        checksum = hashlib.sha256(payload).hexdigest()
        storage_uri = await self._asset_store.put(payload, MediaKind.IMAGE)
        return RenderedAsset(
            storage_uri=storage_uri,
            media_kind=MediaKind.IMAGE,
            format=spec.format,
            checksum=checksum,
            renderer_used=self.name,
            cost_estimate=_cost_from_usage(self._model, body),
            duration_s=None,
            generated_at=datetime.now(UTC),
        )

    async def _generate(self, prompt: str, width: int, height: int) -> dict[str, object]:
        response = await self._http_client.post(
            f"{self._base_url}/images/generations",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "prompt": prompt,
                "size": f"{width}x{height}",
                "n": 1,
            },
            timeout=self._timeout_s,
        )
        response.raise_for_status()
        body: dict[str, object] = response.json()
        return body

    def _decode_first_image(self, body: dict[str, object]) -> bytes:
        data = body.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise OpenAiImageRenderError(f"respuesta sin data[0]: {body!r}")
        b64_json = data[0].get("b64_json")
        if not isinstance(b64_json, str) or not b64_json:
            raise OpenAiImageRenderError(f"respuesta sin b64_json: {body!r}")
        return base64.b64decode(b64_json)

    def _crop_and_resize(self, raw_payload: bytes, spec: ImageSpec) -> bytes:
        with Image.open(io.BytesIO(raw_payload)) as opened:
            rgb_image = opened.convert("RGB")
        box = crop_to_format(
            rgb_image.width, rgb_image.height, spec.format, safe_area=spec.brand_kit.safe_area
        )
        cropped = rgb_image.crop((box.left, box.top, box.left + box.width, box.top + box.height))
        resized = cropped.resize((spec.format.width, spec.format.height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        resized.save(buffer, format="PNG")
        return buffer.getvalue()
