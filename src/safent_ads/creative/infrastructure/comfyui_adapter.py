"""`ComfyUiImageRenderer`/`ComfyUiVideoRenderer` implementan
`ImageRendererPort`/`VideoRendererPort` sobre la API HTTP de ComfyUI
(`infra/creative/workflows/README.md`, threat-model.md C-29: solo loopback
de la DGX, un trabajo pesado a la vez, timeout).

**Cierre en falso 2026-09-09** (research.md D7, `renderer_selector.py`
`RendererTier.LOCAL_GPU`): la difusion local en la DGX tumbo la maquina
cuatro veces el 9-sep (0%->96% de uso de GPU, 70->94 C en 1-2 minutos, una
caida de 54 minutos). Ambos renderizadores exigen `local_enabled=True` en
el constructor — el valor debe venir de
`CreativeSettings.creative_local_enabled` (`ADS_CREATIVE_LOCAL_ENABLED`,
`False` por defecto) — y fallan de inmediato si no, sin intentar tocar la
red. No es una bandera que se pueda pasar por alto en caliente: es la
unica forma de instanciar la clase.

La sustitucion de placeholders es **exactamente** la del README: los
numericos (`width`/`height`/`seed`) se reemplazan incluyendo sus comillas
para que el resultado sea un numero JSON, no una cadena; el texto usa
`json.dumps` para escapar comillas/Unicode correctamente. ComfyUI nunca
expone una URL al llamador: este adaptador descarga los bytes de `/view` y
los entrega ya resueltos via `AssetStorePort.put`."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import httpx
import structlog

from safent_ads.creative.application.errors import CreativeAssetNotFoundError
from safent_ads.creative.application.ports import (
    AssetRetrievalPort,
    AssetStorePort,
    CreativeAssetRepository,
)
from safent_ads.creative.domain.enums import MediaKind, RendererName
from safent_ads.creative.domain.money import Money
from safent_ads.creative.domain.render_specs import ImageSpec, RenderedAsset, VideoSpec
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError

logger = structlog.get_logger(__name__)

_DEFAULT_POLL_INTERVAL_S = 1.0
_LOCAL_RENDER_COST = Money.zero("USD")


class ComfyUiRenderError(InfrastructureError):
    """ComfyUI devolvio un error o un `/history` sin salidas utilizables."""


class ComfyUiTimeoutError(InfrastructureError):
    """El trabajo no termino dentro de `timeout_s` (threat-model.md C-29:
    un trabajo pesado a la vez, con timeout — nunca espera indefinida)."""


class CreativeLocalRenderingDisabledError(InfrastructureError):
    """`local_enabled=False` (por defecto): la difusion local en la DGX
    esta apagada desde el incidente termico del 9-sep-2026 (4 caidas,
    94 C en 1-2 minutos). Habilitala explicitamente con
    `ADS_CREATIVE_LOCAL_ENABLED=true` y permiso por tanda, nunca de forma
    permanente en `.env` de produccion."""


def _require_local_rendering_enabled(*, local_enabled: bool) -> None:
    if not local_enabled:
        raise CreativeLocalRenderingDisabledError(
            "ComfyUI deshabilitado por defecto tras el incidente termico de "
            "la DGX del 9-sep-2026 (4 caidas, 70->94 C en 1-2 minutos). "
            "Requiere ADS_CREATIVE_LOCAL_ENABLED=true y permiso explicito "
            "por tanda (research.md D7)."
        )


def _substitute_numeric(template: str, placeholder: str, value: int) -> str:
    return template.replace(f'"{{{{{placeholder}}}}}"', str(value))


def _substitute_string(template: str, placeholder: str, value: str) -> str:
    return template.replace(f'"{{{{{placeholder}}}}}"', json.dumps(value))


def fill_workflow_template(
    template: str,
    *,
    prompt: str,
    width: int,
    height: int,
    seed: int,
    image_filename: str | None = None,
) -> dict[str, object]:
    """Sustitucion de placeholders `infra/creative/workflows/README.md`:
    los numericos con sus comillas incluidas (para que salga un numero
    JSON), el texto via `json.dumps`. Se aplica antes de parsear, nunca
    despues."""
    filled = _substitute_string(template, "prompt", prompt)
    filled = _substitute_numeric(filled, "width", width)
    filled = _substitute_numeric(filled, "height", height)
    filled = _substitute_numeric(filled, "seed", seed)
    if image_filename is not None:
        filled = _substitute_string(filled, "image", image_filename)
    parsed: dict[str, object] = json.loads(filled)
    return parsed


class _ComfyUiClient:
    """HTTP de bajo nivel compartido por el renderizador de imagen y de
    video: encolar (`/prompt`), sondear (`/history/{id}`), descargar
    (`/view`)."""

    def __init__(self, http_client: httpx.AsyncClient, base_url: str) -> None:
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")

    async def enqueue(self, prompt_graph: dict[str, object]) -> str:
        response = await self._http_client.post(
            f"{self._base_url}/prompt", json={"prompt": prompt_graph}
        )
        response.raise_for_status()
        body = response.json()
        prompt_id = body.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyUiRenderError(f"respuesta de /prompt sin prompt_id: {body!r}")
        return prompt_id

    async def poll_until_done(
        self, prompt_id: str, *, timeout_s: float, poll_interval_s: float
    ) -> dict[str, object]:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        while True:
            history = await self._fetch_history(prompt_id)
            if history is not None:
                return history
            if loop.time() >= deadline:
                raise ComfyUiTimeoutError(f"{prompt_id} no termino en {timeout_s}s")
            await asyncio.sleep(poll_interval_s)

    async def _fetch_history(self, prompt_id: str) -> dict[str, object] | None:
        response = await self._http_client.get(f"{self._base_url}/history/{prompt_id}")
        response.raise_for_status()
        body = response.json()
        entry = body.get(prompt_id)
        if not isinstance(entry, dict) or not entry.get("outputs"):
            return None
        outputs: dict[str, object] = entry["outputs"]
        return outputs

    async def upload_image(self, filename: str, payload: bytes) -> str:
        """`POST /upload/image` (README `{{image}}` note): sube el frame
        clave antes de sustituir su nombre en el workflow. Devuelve el
        nombre que ComfyUI confirma, no el que se propuso."""
        files = {"image": (filename, payload)}
        response = await self._http_client.post(f"{self._base_url}/upload/image", files=files)
        response.raise_for_status()
        body = response.json()
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise ComfyUiRenderError(f"respuesta de /upload/image sin name: {body!r}")
        return name

    async def download_first_output(self, outputs: dict[str, object]) -> bytes:
        file_ref = _first_file_reference(outputs)
        if file_ref is None:
            raise ComfyUiRenderError(f"sin salida descargable en outputs: {outputs!r}")
        params = {
            "filename": file_ref["filename"],
            "subfolder": file_ref.get("subfolder", ""),
            "type": file_ref.get("type", "output"),
        }
        response = await self._http_client.get(f"{self._base_url}/view", params=params)
        response.raise_for_status()
        return response.content


def _first_file_reference(outputs: dict[str, object]) -> dict[str, str] | None:
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for key in ("images", "videos", "gifs"):
            files = node_output.get(key)
            if isinstance(files, list) and files:
                first = files[0]
                if isinstance(first, dict) and "filename" in first:
                    return first
    return None


class ComfyUiImageRenderer:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        workflow_template: str,
        name: RendererName,
        asset_store: AssetStorePort,
        clock: Clock,
        timeout_s: float,
        local_enabled: bool,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        _require_local_rendering_enabled(local_enabled=local_enabled)
        self.name = name
        self._client = _ComfyUiClient(http_client, base_url)
        self._workflow_template = workflow_template
        self._asset_store = asset_store
        self._clock = clock
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    async def render(self, spec: ImageSpec) -> RenderedAsset:
        seed = spec.seed if spec.seed is not None else 0
        graph = fill_workflow_template(
            self._workflow_template,
            prompt=spec.prompt,
            width=spec.format.width,
            height=spec.format.height,
            seed=seed,
        )
        prompt_id = await self._client.enqueue(graph)
        outputs = await self._client.poll_until_done(
            prompt_id, timeout_s=self._timeout_s, poll_interval_s=self._poll_interval_s
        )
        payload = await self._client.download_first_output(outputs)
        checksum = hashlib.sha256(payload).hexdigest()
        storage_uri = await self._asset_store.put(payload, MediaKind.IMAGE)
        logger.info(
            "comfyui_image_rendered", renderer=self.name.value, seed=seed, prompt_id=prompt_id
        )
        return RenderedAsset(
            storage_uri=storage_uri,
            media_kind=MediaKind.IMAGE,
            format=spec.format,
            checksum=checksum,
            renderer_used=self.name,
            cost_estimate=_LOCAL_RENDER_COST,
            duration_s=None,
            generated_at=datetime.now(UTC),
        )


class ComfyUiVideoRenderer:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        workflow_template: str,
        name: RendererName,
        asset_store: AssetStorePort,
        asset_retrieval: AssetRetrievalPort,
        assets: CreativeAssetRepository,
        clock: Clock,
        timeout_s: float,
        local_enabled: bool,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        _require_local_rendering_enabled(local_enabled=local_enabled)
        self.name = name
        self._client = _ComfyUiClient(http_client, base_url)
        self._workflow_template = workflow_template
        self._asset_store = asset_store
        self._asset_retrieval = asset_retrieval
        self._assets = assets
        self._clock = clock
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    async def render(self, spec: VideoSpec) -> RenderedAsset:
        seed = spec.seed if spec.seed is not None else 0
        image_filename = await self._upload_key_frame(spec)
        graph = fill_workflow_template(
            self._workflow_template,
            prompt=spec.motion_prompt,
            width=spec.format.width,
            height=spec.format.height,
            seed=seed,
            image_filename=image_filename,
        )
        prompt_id = await self._client.enqueue(graph)
        outputs = await self._client.poll_until_done(
            prompt_id, timeout_s=self._timeout_s, poll_interval_s=self._poll_interval_s
        )
        payload = await self._client.download_first_output(outputs)
        checksum = hashlib.sha256(payload).hexdigest()
        storage_uri = await self._asset_store.put(payload, MediaKind.VIDEO)
        logger.info(
            "comfyui_video_rendered", renderer=self.name.value, seed=seed, prompt_id=prompt_id
        )
        return RenderedAsset(
            storage_uri=storage_uri,
            media_kind=MediaKind.VIDEO,
            format=spec.format,
            checksum=checksum,
            renderer_used=self.name,
            cost_estimate=_LOCAL_RENDER_COST,
            duration_s=float(spec.duration_s),
            generated_at=datetime.now(UTC),
        )

    async def _upload_key_frame(self, spec: VideoSpec) -> str:
        if not spec.key_frames:
            raise ComfyUiRenderError("VideoSpec.key_frames vacio: i2v_ltx.json requiere un frame")
        key_frame_asset_id = spec.key_frames[0]
        asset = await self._assets.get(key_frame_asset_id)
        if asset is None:
            raise CreativeAssetNotFoundError(str(key_frame_asset_id))
        payload = await self._asset_retrieval.get(asset.storage_uri)
        return await self._client.upload_image(f"{key_frame_asset_id}.png", payload)
