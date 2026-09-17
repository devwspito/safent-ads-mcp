"""`ImportCreativeAsset` (tool-surface.md §2.2 P1 `import_creative_asset`):
cierra el circulo de la delegacion a herramientas nativas
(`infrastructure/hermes_tool_renderer.py`, `application/delegation.py`).
Trae el activo que el agente acaba de producir a nuestro almacen
trazable: valida el host de origen (allow-list, threat-model.md C-11/C-12),
lo descarga una vez, y de ahi en adelante se referencia solo por
`AssetId`/`StorageUri` — nunca por la URL libre otra vez.

`AssetStorePort.put` ya aplica tamano maximo y magic bytes
(`LocalAssetStorage`); este caso de uso no repite esa validacion, solo
anade la que le es propia: el allow-list de host de origen, ANTES de
intentar ninguna conexion (fail closed).

`from_bytes` (004 tasks-2.md W4, historia 13): gemelo de `brand.application.
upload_brand_asset.UploadBrandAsset.from_bytes` -- el arnes ya tiene los
bytes (los genero el o se los dio la persona, decodificados de base64 en
el borde MCP, D-2: nunca una URL libre), asi que no hay host que
comprobar; solo el `AssetStorePort.put` que ya aplica tamano y magic
bytes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from safent_ads.creative.application.errors import CreativeBriefNotFoundError
from safent_ads.creative.application.ports import (
    AssetFetchPort,
    AssetStorePort,
    CreativeAssetRepository,
    CreativeBriefRepository,
)
from safent_ads.creative.domain.asset_import import validate_import_source_url
from safent_ads.creative.domain.creative_asset import CreativeAsset, Provenance
from safent_ads.creative.domain.enums import GenerationStatus, MediaKind, RendererName
from safent_ads.creative.domain.identifiers import AssetId, BriefId, SignalId
from safent_ads.creative.domain.money import Money
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

_IMPORTED_ASSET_COST = Money.zero("USD")  # el coste real lo pago el propietario via su suscripcion

_IMPORTED_RENDERER_NAME = RendererName.HERMES_NATIVE_DELEGATED


@dataclass(frozen=True, kw_only=True)
class ImportCreativeAssetRequest:
    business_id: BusinessId
    brief_id: BriefId
    source_signal_id: SignalId | None
    source_url: str
    media_kind: MediaKind
    native_tool_used: str


@dataclass(frozen=True, kw_only=True)
class ImportCreativeAssetFromBytesRequest:
    business_id: BusinessId
    brief_id: BriefId
    media_kind: MediaKind
    payload: bytes
    native_tool_used: str


class ImportCreativeAsset:
    def __init__(
        self,
        *,
        briefs: CreativeBriefRepository,
        assets: CreativeAssetRepository,
        asset_fetch: AssetFetchPort,
        asset_store: AssetStorePort,
        allowed_hosts: frozenset[str],
        clock: Clock,
    ) -> None:
        self._briefs = briefs
        self._assets = assets
        self._asset_fetch = asset_fetch
        self._asset_store = asset_store
        self._allowed_hosts = allowed_hosts
        self._clock = clock

    async def execute(self, request: ImportCreativeAssetRequest) -> AssetId:
        if await self._briefs.get(request.brief_id) is None:
            raise CreativeBriefNotFoundError(str(request.brief_id))
        validate_import_source_url(request.source_url, self._allowed_hosts)
        payload = await self._asset_fetch.fetch(request.source_url)
        asset = await self._store_and_build_asset(
            business_id=request.business_id,
            brief_id=request.brief_id,
            media_kind=request.media_kind,
            native_tool_used=request.native_tool_used,
            source_signal_id=request.source_signal_id,
            payload=payload,
        )
        await self._assets.add(asset)
        return asset.asset_id

    async def from_bytes(self, request: ImportCreativeAssetFromBytesRequest) -> CreativeAsset:
        if await self._briefs.get(request.brief_id) is None:
            raise CreativeBriefNotFoundError(str(request.brief_id))
        asset = await self._store_and_build_asset(
            business_id=request.business_id,
            brief_id=request.brief_id,
            media_kind=request.media_kind,
            native_tool_used=request.native_tool_used,
            source_signal_id=None,
            payload=request.payload,
        )
        await self._assets.add(asset)
        return asset

    async def _store_and_build_asset(
        self,
        *,
        business_id: BusinessId,
        brief_id: BriefId,
        media_kind: MediaKind,
        native_tool_used: str,
        source_signal_id: SignalId | None,
        payload: bytes,
    ) -> CreativeAsset:
        checksum = hashlib.sha256(payload).hexdigest()
        storage_uri = await self._asset_store.put(payload, media_kind)
        provenance = Provenance(
            renderer_used=_IMPORTED_RENDERER_NAME,
            model_name=native_tool_used,
            seed=None,
            brief_id=brief_id,
            source_signal_id=source_signal_id,
            generation_status=GenerationStatus.MODEL_GENERATED,
            generated_at=self._clock.now(),
        )
        return CreativeAsset(
            asset_id=AssetId.new(),
            business_id=business_id,
            media_kind=media_kind,
            # El aspecto real del activo importado no se ha cotejado
            # todavia contra ningun `Format` de anuncio — eso sucede en un
            # paso de composicion posterior (`ComposeBanner`/
            # `domain/image_crop.py`), no aqui.
            format=None,
            duration_seconds=None,
            storage_uri=storage_uri,
            checksum=checksum,
            cost_estimate=_IMPORTED_ASSET_COST,
            provenance=provenance,
        )
