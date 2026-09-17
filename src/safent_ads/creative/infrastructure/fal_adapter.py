"""`FalVideoRenderer` implementa `VideoRendererPort` sobre la API de cola
de fal.ai (respaldo cloud de video, research/content-generation-stack.md
§6: Wan 2.5 $0.05/s). Patron `submit -> poll status -> fetch result`,
mismo criterio de timeout que `ComfyUiVideoRenderer`. La clave vive en
`BrokerSettings` (threat-model.md C-29)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from safent_ads.creative.application.ports import AssetStorePort
from safent_ads.creative.domain.enums import MediaKind, RendererName
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import RenderedAsset, VideoSpec
from safent_ads.shared.errors import InfrastructureError

_FAL_COST_PER_SECOND = Decimal("0.05")
_DEFAULT_MODEL_PATH = "fal-ai/wan-2.5/image-to-video"
_DEFAULT_POLL_INTERVAL_S = 2.0


class FalRenderError(InfrastructureError):
    """fal.ai devolvio un error o un resultado sin video."""


class FalTimeoutError(InfrastructureError):
    """El trabajo no termino dentro de `timeout_s`."""


class FalVideoRenderer:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        api_key: str,
        asset_store: AssetStorePort,
        model_path: str = _DEFAULT_MODEL_PATH,
        base_url: str = "https://queue.fal.run",
        timeout_s: float = 300.0,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self.name = RendererName.WAN_2_2
        self._http_client = http_client
        self._api_key = api_key
        self._asset_store = asset_store
        self._model_path = model_path
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    async def render(self, spec: VideoSpec) -> RenderedAsset:
        request_id = await self._submit(spec)
        result = await self._poll_until_done(request_id)
        payload = await self._download_video(result)
        checksum = hashlib.sha256(payload).hexdigest()
        storage_uri = await self._asset_store.put(payload, MediaKind.VIDEO)
        cost = Money(_FAL_COST_PER_SECOND * spec.duration_s, "USD")
        return RenderedAsset(
            storage_uri=storage_uri,
            media_kind=MediaKind.VIDEO,
            format=spec.format,
            checksum=checksum,
            renderer_used=self.name,
            cost_estimate=cost,
            duration_s=float(spec.duration_s),
            generated_at=datetime.now(UTC),
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Key {self._api_key}"}

    async def _submit(self, spec: VideoSpec) -> str:
        response = await self._http_client.post(
            f"{self._base_url}/{self._model_path}",
            headers=self._headers(),
            json={"prompt": spec.motion_prompt, "duration": spec.duration_s},
        )
        response.raise_for_status()
        body = response.json()
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise FalRenderError(f"respuesta sin request_id: {body!r}")
        return request_id

    async def _poll_until_done(self, request_id: str) -> dict[str, object]:
        status_url = f"{self._base_url}/{self._model_path}/requests/{request_id}/status"
        result_url = f"{self._base_url}/{self._model_path}/requests/{request_id}"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._timeout_s
        while True:
            status_response = await self._http_client.get(status_url, headers=self._headers())
            status_response.raise_for_status()
            status = status_response.json().get("status")
            if status == "COMPLETED":
                result_response = await self._http_client.get(result_url, headers=self._headers())
                result_response.raise_for_status()
                result: dict[str, object] = result_response.json()
                return result
            if loop.time() >= deadline:
                raise FalTimeoutError(f"{request_id} no termino en {self._timeout_s}s")
            await asyncio.sleep(self._poll_interval_s)

    async def _download_video(self, result: dict[str, object]) -> bytes:
        # `url` sale de la propia respuesta de fal.ai (proveedor cloud ya
        # configurado, no un argumento de tool ni entrada del modelo): el
        # allow-list de egreso del broker (threat-model.md C-12) debe
        # incluir el dominio de CDN de fal.ai antes de habilitar este
        # respaldo en produccion.
        video = result.get("video")
        url = video.get("url") if isinstance(video, dict) else None
        if not isinstance(url, str) or not url:
            raise FalRenderError(f"resultado sin video.url: {result!r}")
        response = await self._http_client.get(url)
        response.raise_for_status()
        return response.content
