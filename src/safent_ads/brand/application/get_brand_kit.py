"""`GetBrandKit`: caso de uso que respalda la herramienta MCP `get_brand_kit`
y el endpoint REST equivalente (tool-surface.md §2.2, §6 P1)."""

from __future__ import annotations

from safent_ads.brand.application.errors import BrandKitNotFoundError
from safent_ads.brand.application.ports import BrandKitRepository
from safent_ads.brand.domain.brand_kit import BrandKit
from safent_ads.shared.ids import BusinessId


class GetBrandKit:
    def __init__(self, brand_kits: BrandKitRepository) -> None:
        self._brand_kits = brand_kits

    async def execute(self, business_id: BusinessId) -> BrandKit:
        brand_kit = await self._brand_kits.get_by_business(business_id)
        if brand_kit is None:
            raise BrandKitNotFoundError(f"sin kit de marca para el negocio {business_id}")
        return brand_kit
