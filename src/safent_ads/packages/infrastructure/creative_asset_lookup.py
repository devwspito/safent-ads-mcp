"""`PackageCreativeAssetLookup`: implementa `CreativeAssetLookupPort` sobre
`creative` (dependencia permitida, `packages -> {..., creative, ...}`).

Repite la MISMA comprobacion (negocio + `READY` + veredicto `PASS` +
`MediaKind.IMAGE`) que ME-7 exige verificar tres veces (al proponer, al
aprobar, antes del paso del anuncio) -- esta clase es el unico punto donde
esa comprobacion vive, para que las tres llamadas nunca diverjan.

El `mime_type` no es una columna de `creative_assets`: se obtiene
esnifando los primeros bytes reales del activo, mismo criterio que
`creative.infrastructure.creative_preview_content_type` (nunca la
extension del fichero guardado)."""

from __future__ import annotations

from safent_ads.creative.application.ports import AssetRetrievalPort, CreativeAssetRepository
from safent_ads.creative.domain.enums import CreativeAssetState, MediaKind, PolicyVerdictResult
from safent_ads.creative.domain.identifiers import AssetId, AssetIdFormatError
from safent_ads.creative.infrastructure.creative_preview_content_type import (
    UnpreviewableCreativeAssetError,
    resolve_creative_preview_content_type,
)
from safent_ads.packages.application.ports import CreativeAssetSnapshot
from safent_ads.shared.ids import BusinessId

__all__ = ["PackageCreativeAssetLookup"]


class PackageCreativeAssetLookup:
    """Depende de los puertos de `creative.application`, nunca de su
    adaptador SQL concreto (DIP): `composition/app.py` inyecta
    `RequestScopedCreativeAssetRepository`/`LocalAssetStorage` reales; los
    tests unitarios inyectan los dobles de `tests.unit.creative.
    infrastructure.fakes`."""

    def __init__(
        self, assets: CreativeAssetRepository, asset_retrieval: AssetRetrievalPort
    ) -> None:
        self._assets = assets
        self._asset_retrieval = asset_retrieval

    async def find_usable(
        self, *, business_id: BusinessId, asset_id: str
    ) -> CreativeAssetSnapshot | None:
        try:
            parsed_id = AssetId.parse(asset_id)
        except AssetIdFormatError:
            return None
        asset = await self._assets.get(parsed_id)
        if (
            asset is None
            or asset.business_id != business_id
            or asset.media_kind is not MediaKind.IMAGE
            or asset.state is not CreativeAssetState.READY
            or asset.policy_verdict is None
            or asset.policy_verdict.verdict is not PolicyVerdictResult.PASS_
            or asset.format is None
        ):
            return None
        try:
            payload = await self._asset_retrieval.get(asset.storage_uri)
            content_type = resolve_creative_preview_content_type(payload).content_type
        except UnpreviewableCreativeAssetError:
            return None
        return CreativeAssetSnapshot(
            asset_id=str(asset.asset_id),
            checksum=asset.checksum,
            mime_type=content_type,
            width=asset.format.width,
            height=asset.format.height,
            preview_key=str(asset.storage_uri),
        )

    async def get_bytes(self, *, business_id: BusinessId, asset_id: str) -> bytes | None:
        """Implementa `CreativeAssetBytesPort` (T111, BL-6): los bytes
        reales, solo tras la MISMA comprobacion que `find_usable` -- un
        activo que no pasa esa puerta no sube nunca, ni siquiera para
        verificar su `sha256` (`None` es la unica respuesta para
        inexistente/ajeno/no listo, igual que ME-7)."""
        snapshot = await self.find_usable(business_id=business_id, asset_id=asset_id)
        if snapshot is None:
            return None
        asset = await self._assets.get(AssetId.parse(asset_id))
        if asset is None:  # pragma: no cover - find_usable ya lo confirmo instantes antes
            return None
        return await self._asset_retrieval.get(asset.storage_uri)
