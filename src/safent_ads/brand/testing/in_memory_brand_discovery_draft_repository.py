"""Implementacion en memoria de `BrandDiscoveryDraftRepository` para tests
de los casos de uso de `application` sin depender de Postgres/SQLAlchemy."""

from __future__ import annotations

from collections.abc import Iterable

from safent_ads.brand.domain.discovery import BrandDiscoveryDraft
from safent_ads.shared.ids import BusinessId


class InMemoryBrandDiscoveryDraftRepository:
    def __init__(self, drafts: Iterable[BrandDiscoveryDraft] = ()) -> None:
        self._by_business: dict[BusinessId, BrandDiscoveryDraft] = {
            d.business_id: d for d in drafts
        }

    async def get_by_business(self, business_id: BusinessId) -> BrandDiscoveryDraft | None:
        return self._by_business.get(business_id)

    async def save(self, draft: BrandDiscoveryDraft) -> None:
        self._by_business[draft.business_id] = draft
