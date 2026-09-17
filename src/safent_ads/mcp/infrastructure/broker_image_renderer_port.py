"""`BrokerImageRenderer` implementa `ImageRendererPort`
(`creative/application/ports.py`) delegando el render real en `ads-broker`
sobre el op `render_image` del socket (threat-model.md C-29:
`FAL_API_KEY`/`OPENAI_API_KEY` viven solo en `BrokerSettings`, `ads-api`
nunca las ve). Un renderizador de imagen mas para `GenerateCreativeAssets`
-- la cascada entre proveedores (`RendererSelector.candidates_for`) sigue
viviendo ahi: esta clase solo ejecuta el candidato que se le pide, nunca
elige uno ella misma. Los bytes decodificados se guardan con el MISMO
`AssetStorePort` (`LocalAssetStorage`, `composition/app.py::
_build_creative_asset_store`) que ya sirve `/creatives`/previews -- un
activo generado por el broker vive en el mismo almacen trazable que uno
generado localmente.

`ImageRenderBrokerClient` extiende `BrokerSocketClient` (mismo patron que
`MetaReferenceDataBrokerClient`/`GoogleKeywordIdeaBrokerClient`,
`broker_reference_data_port.py`): reutiliza su framing/`_request`, solo
anade el propio op. `max_frame_bytes`/`timeout_seconds` se elevan sobre el
resto de clientes del broker (`composition/broker.py::_MAX_FRAME_BYTES`,
mismo numero): una imagen en base64 pesa mas que cualquier otra respuesta
del socket, y el ciclo submit->poll->descarga puede tardar hasta el
`render_timeout_s` propio del broker."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from safent_ads.accounts.application.errors import BrokerRequestDeniedError
from safent_ads.accounts.infrastructure.broker_client import BrokerSocketClient
from safent_ads.creative.application.errors import (
    RenderBudgetExceededError,
    RenderQuotaExceededError,
)
from safent_ads.creative.application.ports import (
    AssetStorePort,
    current_render_business_id,
)
from safent_ads.creative.domain.enums import MediaKind, RendererName
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset

# M-2 (revision de seguridad 0.2.22): `RenderImageService` (ads-broker)
# denuncia estos dos con codigos propios, distintos de una simple
# indisponibilidad de proveedor -- se traducen a excepciones de `creative`
# (bounded context correcto para quien llama a `ImageRendererPort`, nunca
# un `BrokerRequestDeniedError` crudo de `accounts`) para que
# `GenerateCreativeAssets` los pare en vez de tratarlos como "prueba el
# siguiente candidato".
_QUOTA_ERROR_CODE: Final = "RENDER_QUOTA_EXCEEDED"
_COST_CAP_ERROR_CODE: Final = "RENDER_COST_CAP"

__all__ = ["BrokerImageRenderer", "ImageRenderBrokerClient"]

_MAX_FRAME_BYTES: Final = 16 * 1024 * 1024
_TIMEOUT_SECONDS: Final = 180.0


class ImageRenderBrokerClient(BrokerSocketClient):
    def __init__(self, socket_path: Path) -> None:
        super().__init__(
            socket_path, max_frame_bytes=_MAX_FRAME_BYTES, timeout_seconds=_TIMEOUT_SECONDS
        )

    async def render_image(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Nunca `retryable=True`: `render_image` gasta dinero real (proveedor
        # FAL/OpenAI) y consume la cuota por negocio de `RenderImageService`
        # ANTES de que la respuesta cruce el socket -- si el broker ya
        # genero la imagen y solo la escritura de la trama fallo (la misma
        # carrera que motiva el reintento en las lecturas), una segunda
        # conexion repetiria el gasto y la cuota, no solo la lectura. Un
        # `BrokerConnectionError` aqui debe seguir siendo un fallo real, sin
        # reintento automatico.
        result: dict[str, Any] = await self._request(payload)
        return result


def _brand_kit_fields(spec: ImageSpec) -> dict[str, Any]:
    brand_kit = spec.brand_kit
    safe_area = brand_kit.safe_area
    return {
        "primary_font": brand_kit.primary_font,
        "secondary_font": brand_kit.secondary_font,
        "primary_color_hex": brand_kit.primary_color_hex,
        "secondary_color_hex": brand_kit.secondary_color_hex,
        "logo_asset_id": str(brand_kit.logo_asset_id),
        "safe_area_top": safe_area.top,
        "safe_area_bottom": safe_area.bottom,
        "safe_area_left": safe_area.left,
        "safe_area_right": safe_area.right,
    }


class BrokerImageRenderer:
    def __init__(
        self, name: RendererName, client: ImageRenderBrokerClient, asset_store: AssetStorePort
    ) -> None:
        self.name = name
        self._client = client
        self._asset_store = asset_store

    async def render(self, spec: ImageSpec) -> RenderedAsset:
        # `reference_assets` no cruza el wire todavia (ver docstring de
        # `RenderImageRequest`, broker/presentation/request_schemas.py):
        # ningun llamante de `GenerateCreativeAssets` los manda hoy.
        #
        # `business_id`: M-2 (revision de seguridad 0.2.22), viaja por
        # `creative.application.ports.render_call_scope` -- `GenerateCreative
        # Assets` lo fija alrededor de TODA la cascada de candidatos (nunca
        # `None` en el unico llamador real de hoy). `RenderImageService`
        # (ads-broker) exige el campo y aplica cuota por negocio con el.
        response = await self._request_render(
            {
                "op": "render_image",
                "business_id": current_render_business_id(),
                "renderer": self.name.value,
                "prompt": spec.prompt,
                "format": spec.format.value,
                "seed": spec.seed,
                "brand_kit": _brand_kit_fields(spec),
            }
        )
        return await self._build_rendered_asset(spec, response)

    async def _request_render(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._client.render_image(payload)
        except BrokerRequestDeniedError as exc:
            if exc.error_code == _QUOTA_ERROR_CODE:
                raise RenderQuotaExceededError(str(exc)) from exc
            if exc.error_code == _COST_CAP_ERROR_CODE:
                raise RenderBudgetExceededError(str(exc)) from exc
            raise

    async def _build_rendered_asset(
        self, spec: ImageSpec, response: dict[str, Any]
    ) -> RenderedAsset:
        # El checksum se recalcula aqui, nunca se confia en el que manda el
        # broker (`provenance.checksum`, solo informativo) -- mismo criterio
        # que `FalImageRenderer`/`OpenAiImageRenderer`: el checksum describe
        # los bytes que ESTE proceso acaba de guardar, no los que otro
        # proceso dice haber generado.
        payload = base64.b64decode(response["image_base64"])
        checksum = hashlib.sha256(payload).hexdigest()
        storage_uri = await self._asset_store.put(payload, MediaKind.IMAGE)
        cost = response["cost"]
        provenance = response["provenance"]
        return RenderedAsset(
            storage_uri=storage_uri,
            media_kind=MediaKind.IMAGE,
            format=spec.format,
            checksum=checksum,
            renderer_used=RendererName(provenance["renderer"]),
            cost_estimate=Money(Decimal(cost["amount"]), cost["currency"]),
            duration_s=provenance["duration_s"],
            generated_at=datetime.fromisoformat(provenance["generated_at"]),
        )
