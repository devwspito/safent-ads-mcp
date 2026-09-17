"""`ListBrandAssets`: filtra por tipo, respalda `list_brand_assets`."""

from __future__ import annotations

import pytest

from safent_ads.brand.application.errors import BrandKitNotFoundError
from safent_ads.brand.application.list_brand_assets import ListBrandAssets
from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.testing.in_memory_brand_kit_repository import InMemoryBrandKitRepository
from safent_ads.shared.ids import BusinessId
from tests.unit.brand.factories import make_brand_kit


async def test_lists_all_assets_by_default() -> None:
    business_id = BusinessId.new()
    logo = BrandAsset(
        asset_id="logo-1", kind=AssetKind.LOGO_VECTOR, storage_uri="s3://logo.svg", usage_rule="x"
    )
    photo = BrandAsset(
        asset_id="photo-1",
        kind=AssetKind.REFERENCE_PHOTO,
        storage_uri="s3://photo.jpg",
        usage_rule="x",
    )
    kit = make_brand_kit(business_id=business_id, assets=(logo, photo))
    use_case = ListBrandAssets(InMemoryBrandKitRepository([kit]))

    result = await use_case.execute(business_id)

    assert result == (logo, photo)


async def test_filters_by_kind() -> None:
    business_id = BusinessId.new()
    logo = BrandAsset(
        asset_id="logo-1", kind=AssetKind.LOGO_VECTOR, storage_uri="s3://logo.svg", usage_rule="x"
    )
    photo = BrandAsset(
        asset_id="photo-1",
        kind=AssetKind.REFERENCE_PHOTO,
        storage_uri="s3://photo.jpg",
        usage_rule="x",
    )
    kit = make_brand_kit(business_id=business_id, assets=(logo, photo))
    use_case = ListBrandAssets(InMemoryBrandKitRepository([kit]))

    result = await use_case.execute(business_id, kind=AssetKind.REFERENCE_PHOTO)

    assert result == (photo,)


async def test_raises_when_business_has_no_kit() -> None:
    use_case = ListBrandAssets(InMemoryBrandKitRepository())

    with pytest.raises(BrandKitNotFoundError):
        await use_case.execute(BusinessId.new())
