"""`ListBrandAssets`: caso de uso que respalda la herramienta MCP
`list_brand_assets` (tool-surface.md §2.2: "Logotipos y fotos aprobadas
como referencia de edicion")."""

from __future__ import annotations

from safent_ads.brand.application.errors import BrandKitNotFoundError
from safent_ads.brand.application.ports import BrandKitRepository
from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.shared.ids import BusinessId


class ListBrandAssets:
    def __init__(self, brand_kits: BrandKitRepository) -> None:
        self._brand_kits = brand_kits

    async def execute(
        self, business_id: BusinessId, *, kind: AssetKind | None = None
    ) -> tuple[BrandAsset, ...]:
        brand_kit = await self._brand_kits.get_by_business(business_id)
        if brand_kit is None:
            raise BrandKitNotFoundError(f"sin kit de marca para el negocio {business_id}")
        return brand_kit.assets_of_kind(kind)
