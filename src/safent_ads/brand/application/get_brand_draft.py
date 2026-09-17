"""`GetBrandDraft`: caso de uso que respalda la herramienta MCP
`get_brand_draft` y `GET /api/v1/brand/draft` -- lo que el agente lee para
proponer tono de voz a partir de los ejemplos de copy, y lo que el
propietario revisa antes de `ConfirmBrandDraft`."""

from __future__ import annotations

from safent_ads.brand.application.errors import BrandDraftNotFoundError
from safent_ads.brand.application.ports import BrandDiscoveryDraftRepository
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft
from safent_ads.shared.ids import BusinessId


class GetBrandDraft:
    def __init__(self, drafts: BrandDiscoveryDraftRepository) -> None:
        self._drafts = drafts

    async def execute(self, business_id: BusinessId) -> BrandDiscoveryDraft:
        draft = await self._drafts.get_by_business(business_id)
        if draft is None:
            raise BrandDraftNotFoundError(f"sin borrador de marca para el negocio {business_id}")
        return draft
