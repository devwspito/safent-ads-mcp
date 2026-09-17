"""Implementacion en memoria de `BrandKitRepository` para tests de los
casos de uso de `application` sin depender de Postgres/SQLAlchemy."""

from __future__ import annotations

from collections.abc import Iterable

from safent_ads.brand.domain.brand_kit import BrandKit
from safent_ads.shared.ids import BusinessId


class InMemoryBrandKitRepository:
    def __init__(self, brand_kits: Iterable[BrandKit] = ()) -> None:
        self._by_business: dict[BusinessId, BrandKit] = {k.business_id: k for k in brand_kits}

    async def get_by_business(self, business_id: BusinessId) -> BrandKit | None:
        return self._by_business.get(business_id)

    async def save(self, brand_kit: BrandKit) -> None:
        self._by_business[brand_kit.business_id] = brand_kit
