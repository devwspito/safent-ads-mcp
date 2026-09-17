"""`UploadBrandAsset`: dos caminos deliberadamente separados (sin flag
booleano que cambie el comportamiento) para la ruta manual del owner
request ("El usuario puede subir manual"):

- `from_bytes` -- el panel sube bytes de verdad (`POST /api/v1/brand/assets`,
  multipart). Unico lugar de `brand` que recibe un payload binario.
- `from_existing_candidate` -- la herramienta MCP `upload_brand_asset`
  SOLO recibe un `asset_id` ya importado (contracts/mcp-tools.md,
  threat-model.md C-11: "no free URLs", y por extension aqui tampoco bytes
  libres via MCP): reclasifica un candidato que YA esta en el borrador
  (subido a mano o rastreado) con el `kind` que decide el propietario/agente.

Ambos caminos anaden el resultado como candidato MANUAL_UPLOAD al mismo
`BrandDiscoveryDraft` (creando uno vacio si no existe todavia): la
confirmacion final siempre pasa por `ConfirmBrandDraft`, sea cual sea el
origen del dato."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from ulid import ULID

from safent_ads.brand.application.errors import BrandDraftAssetNotFoundError
from safent_ads.brand.application.ports import BrandAssetStoragePort, BrandDiscoveryDraftRepository
from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft, DiscoverySource, LogoCandidate
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

_MANUAL_UPLOAD_CONFIDENCE = 1.0


@dataclass(frozen=True, kw_only=True)
class UploadBrandAssetBytesRequest:
    business_id: BusinessId
    kind: AssetKind
    payload: bytes


@dataclass(frozen=True, kw_only=True)
class SelectExistingBrandAssetRequest:
    business_id: BusinessId
    asset_id: str
    kind: AssetKind


class UploadBrandAsset:
    def __init__(
        self,
        *,
        drafts: BrandDiscoveryDraftRepository,
        storage: BrandAssetStoragePort,
        clock: Clock,
    ) -> None:
        self._drafts = drafts
        self._storage = storage
        self._clock = clock

    async def from_bytes(self, request: UploadBrandAssetBytesRequest) -> LogoCandidate:
        storage_uri = await self._storage.put(request.payload, request.kind)
        candidate = LogoCandidate(
            asset_id=_new_asset_id(),
            kind=request.kind,
            storage_uri=storage_uri,
            sha256=_sha256_hex(request.payload),
            source=DiscoverySource.MANUAL_UPLOAD,
            confidence=_MANUAL_UPLOAD_CONFIDENCE,
        )
        await self._append_to_draft(request.business_id, candidate)
        return candidate

    async def from_existing_candidate(
        self, request: SelectExistingBrandAssetRequest
    ) -> LogoCandidate:
        draft = await self._drafts.get_by_business(request.business_id)
        existing = draft.logo_by_asset_id(request.asset_id) if draft is not None else None
        if existing is None:
            raise BrandDraftAssetNotFoundError(
                f"asset_id {request.asset_id!r} no esta en el borrador actual"
            )
        reclassified = LogoCandidate(
            asset_id=existing.asset_id,
            kind=request.kind,
            storage_uri=existing.storage_uri,
            sha256=existing.sha256,
            source=existing.source,
            confidence=_MANUAL_UPLOAD_CONFIDENCE,
        )
        await self._append_to_draft(request.business_id, reclassified)
        return reclassified

    async def _append_to_draft(self, business_id: BusinessId, candidate: LogoCandidate) -> None:
        draft = await self._drafts.get_by_business(business_id)
        if draft is None:
            draft = _empty_draft(business_id, self._clock.now())
        await self._drafts.save(draft.with_manual_logo(candidate))


def _empty_draft(business_id: BusinessId, now: datetime) -> BrandDiscoveryDraft:
    return BrandDiscoveryDraft(business_id=business_id, source_url=None, discovered_at=now)


def _new_asset_id() -> str:
    return str(ULID())


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
