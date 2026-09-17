"""`GetBrandKit`: negocio sin kit reporta el hueco, nunca inventa uno."""

from __future__ import annotations

import pytest

from safent_ads.brand.application.errors import BrandKitNotFoundError
from safent_ads.brand.application.get_brand_kit import GetBrandKit
from safent_ads.brand.testing.in_memory_brand_kit_repository import InMemoryBrandKitRepository
from safent_ads.shared.ids import BusinessId
from tests.unit.brand.factories import make_brand_kit


async def test_returns_the_stored_kit() -> None:
    business_id = BusinessId.new()
    kit = make_brand_kit(business_id=business_id)
    use_case = GetBrandKit(InMemoryBrandKitRepository([kit]))

    result = await use_case.execute(business_id)

    assert result is kit


async def test_raises_when_business_has_no_kit() -> None:
    use_case = GetBrandKit(InMemoryBrandKitRepository())

    with pytest.raises(BrandKitNotFoundError):
        await use_case.execute(BusinessId.new())
