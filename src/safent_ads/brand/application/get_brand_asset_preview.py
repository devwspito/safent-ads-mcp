"""`GetBrandAssetPreview`: caso de uso que respalda
`GET /api/v1/brand/assets/{asset_id}/preview` (Marca section del panel:
logos subidos o rastreados sin URL servible todavia,
`svg_sanitizer.py` docstring: "el dia que el panel los sirva").

Resuelve `asset_id` -> `storage_uri` buscando SOLO entre los activos que
ya pertenecen al `business_id` autorizado (kit confirmado primero,
candidatos del borrador despues) y entrega los bytes en crudo via
`BrandAssetStoragePort.get` -- el `asset_id` de la peticion HTTP nunca
llega a una ruta de fichero, solo al `storage_uri` opaco que el propio
dominio genero al guardarlo (threat-model.md C-27, defensa contra path
traversal). Traducir esos bytes a un `Content-Type` HTTP seguro (sniff de
magic bytes, saneado de SVG) es responsabilidad de la presentacion/
infraestructura, no de este caso de uso -- mismo reparto que
`brand/infrastructure/asset_preview_content_type.py` documenta."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.brand.application.errors import BrandAssetNotFoundError
from safent_ads.brand.application.ports import (
    BrandAssetStoragePort,
    BrandDiscoveryDraftRepository,
    BrandKitRepository,
)
from safent_ads.brand.domain.brand_kit import BrandKit
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, kw_only=True)
class BrandAssetPreviewRequest:
    business_id: BusinessId
    asset_id: str


@dataclass(frozen=True, kw_only=True, slots=True)
class BrandAssetPreview:
    payload: bytes


class GetBrandAssetPreview:
    def __init__(
        self,
        *,
        brand_kits: BrandKitRepository,
        drafts: BrandDiscoveryDraftRepository,
        storage: BrandAssetStoragePort,
    ) -> None:
        self._brand_kits = brand_kits
        self._drafts = drafts
        self._storage = storage

    async def execute(self, request: BrandAssetPreviewRequest) -> BrandAssetPreview:
        storage_uri = await self._resolve_storage_uri(request.business_id, request.asset_id)
        if storage_uri is None:
            raise BrandAssetNotFoundError(
                f"asset_id {request.asset_id!r} no pertenece al negocio {request.business_id}"
            )
        payload = await self._storage.get(storage_uri)
        return BrandAssetPreview(payload=payload)

    async def _resolve_storage_uri(self, business_id: BusinessId, asset_id: str) -> str | None:
        kit = await self._brand_kits.get_by_business(business_id)
        from_kit = self._storage_uri_from_kit(kit, asset_id)
        if from_kit is not None:
            return from_kit
        draft = await self._drafts.get_by_business(business_id)
        return self._storage_uri_from_draft(draft, asset_id)

    def _storage_uri_from_kit(self, kit: BrandKit | None, asset_id: str) -> str | None:
        if kit is None:
            return None
        asset = kit.asset_by_id(asset_id)
        return asset.storage_uri if asset is not None else None

    def _storage_uri_from_draft(
        self, draft: BrandDiscoveryDraft | None, asset_id: str
    ) -> str | None:
        if draft is None:
            return None
        candidate = draft.logo_by_asset_id(asset_id)
        return candidate.storage_uri if candidate is not None else None
