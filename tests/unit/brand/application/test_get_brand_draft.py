"""`GetBrandDraft`: negocio sin borrador reporta el hueco, nunca inventa
uno (mismo criterio que `GetBrandKit`)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.brand.application.errors import BrandDraftNotFoundError
from safent_ads.brand.application.get_brand_draft import GetBrandDraft
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft
from safent_ads.brand.testing.in_memory_brand_discovery_draft_repository import (
    InMemoryBrandDiscoveryDraftRepository,
)
from safent_ads.shared.ids import BusinessId

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


async def test_returns_the_stored_draft() -> None:
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(business_id=business_id, source_url=None, discovered_at=_NOW)
    use_case = GetBrandDraft(InMemoryBrandDiscoveryDraftRepository([draft]))

    result = await use_case.execute(business_id)

    assert result == draft


async def test_raises_when_no_draft_exists() -> None:
    use_case = GetBrandDraft(InMemoryBrandDiscoveryDraftRepository())

    with pytest.raises(BrandDraftNotFoundError):
        await use_case.execute(BusinessId.new())
