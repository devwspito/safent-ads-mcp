"""`ContainerCreativeUploadAdapter`: implementa `CreativeUploadPort`
(mcp.application) sobre `ImportCreativeAsset.from_bytes` +
`AssetStorePort.signed_preview_url` -- mismo patron de traduccion de
errores que `ContainerProposalWriteAdapter` (infra/`creative.application`
-> `mcp.application.errors`), nunca una excepcion de
`creative.infrastructure` cruzando a `presentation`.

Vive en `composition`, no en `mcp/infrastructure/`, por el mismo motivo que
`mcp_write_adapter.py`: cablea piezas de otro bounded context
(`creative`) que `mcp` no puede importar por su cuenta sin invertir el
grafo de dependencias (plan.md §4)."""

from __future__ import annotations

from safent_ads.creative.application.errors import CreativeBriefNotFoundError
from safent_ads.creative.application.import_creative_asset import (
    ImportCreativeAsset,
    ImportCreativeAssetFromBytesRequest,
)
from safent_ads.creative.application.ports import AssetStorePort
from safent_ads.creative.domain.creative_asset import CreativeAsset
from safent_ads.creative.domain.enums import MediaKind
from safent_ads.creative.domain.identifiers import BriefId
from safent_ads.creative.infrastructure.local_asset_storage import (
    AssetDimensionsTooLargeError,
    AssetMagicBytesMismatchError,
    AssetPayloadTooLargeError,
)
from safent_ads.mcp.application.creative_upload_port import CreativeUploadResult
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.application.errors import ToolValidationError as McpValidationError
from safent_ads.shared.ids import BusinessId

__all__ = ["ContainerCreativeUploadAdapter"]

_PREVIEW_TTL_SECONDS = 3600


class ContainerCreativeUploadAdapter:
    def __init__(
        self, import_creative_asset: ImportCreativeAsset, asset_store: AssetStorePort
    ) -> None:
        self._import_creative_asset = import_creative_asset
        self._asset_store = asset_store

    async def upload_creative_asset(
        self,
        *,
        business_id: str,
        brief_id: str,
        media_kind: str,
        content: bytes,
        native_tool_used: str,
    ) -> CreativeUploadResult:
        asset = await self._import(business_id, brief_id, media_kind, content, native_tool_used)
        preview_url = await self._asset_store.signed_preview_url(
            asset.storage_uri, _PREVIEW_TTL_SECONDS
        )
        return CreativeUploadResult(
            asset_id=str(asset.asset_id), media_kind=asset.media_kind.value, preview_url=preview_url
        )

    async def _import(
        self,
        business_id: str,
        brief_id: str,
        media_kind: str,
        content: bytes,
        native_tool_used: str,
    ) -> CreativeAsset:
        try:
            return await self._import_creative_asset.from_bytes(
                ImportCreativeAssetFromBytesRequest(
                    business_id=BusinessId.parse(business_id),
                    brief_id=BriefId.parse(brief_id),
                    media_kind=MediaKind(media_kind),
                    payload=content,
                    native_tool_used=native_tool_used,
                )
            )
        except CreativeBriefNotFoundError as exc:
            raise EntityNotFoundError(brief_id) from exc
        except (
            AssetMagicBytesMismatchError,
            AssetPayloadTooLargeError,
            AssetDimensionsTooLargeError,
        ) as exc:
            raise McpValidationError(str(exc)) from exc
