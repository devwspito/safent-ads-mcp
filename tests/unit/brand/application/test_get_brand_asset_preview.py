"""`GetBrandAssetPreview`: resuelve `asset_id` -> bytes SOLO entre lo que
pertenece al `business_id` autorizado (kit confirmado primero, borrador
despues), y nunca deja pasar un `asset_id` de otro negocio."""

from __future__ import annotations

import pytest

from safent_ads.brand.application.errors import BrandAssetNotFoundError
from safent_ads.brand.application.get_brand_asset_preview import (
    BrandAssetPreviewRequest,
    GetBrandAssetPreview,
)
from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft, DiscoverySource, LogoCandidate
from safent_ads.brand.testing.in_memory_brand_asset_storage import InMemoryBrandAssetStorage
from safent_ads.brand.testing.in_memory_brand_discovery_draft_repository import (
    InMemoryBrandDiscoveryDraftRepository,
)
from safent_ads.brand.testing.in_memory_brand_kit_repository import InMemoryBrandKitRepository
from safent_ads.shared.ids import BusinessId
from tests.unit.brand.factories import FIXED_UPDATED_AT, make_brand_kit

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _use_case(
    *,
    kits: InMemoryBrandKitRepository | None = None,
    drafts: InMemoryBrandDiscoveryDraftRepository | None = None,
    storage: InMemoryBrandAssetStorage | None = None,
) -> GetBrandAssetPreview:
    return GetBrandAssetPreview(
        brand_kits=kits or InMemoryBrandKitRepository(),
        drafts=drafts or InMemoryBrandDiscoveryDraftRepository(),
        storage=storage or InMemoryBrandAssetStorage(),
    )


async def test_returns_the_bytes_of_a_confirmed_kit_asset() -> None:
    business_id = BusinessId.new()
    asset = BrandAsset(
        asset_id="logo-1",
        kind=AssetKind.LOGO_RASTER,
        storage_uri="logo_raster/key-1",
        usage_rule="x",
    )
    kit = make_brand_kit(business_id=business_id, assets=(asset,))
    storage = InMemoryBrandAssetStorage(seed={"logo_raster/key-1": _PNG_BYTES})
    use_case = _use_case(kits=InMemoryBrandKitRepository([kit]), storage=storage)

    preview = await use_case.execute(
        BrandAssetPreviewRequest(business_id=business_id, asset_id="logo-1")
    )

    assert preview.payload == _PNG_BYTES


async def test_returns_the_bytes_of_a_draft_logo_candidate() -> None:
    business_id = BusinessId.new()
    candidate = LogoCandidate(
        asset_id="candidate-1",
        kind=AssetKind.LOGO_RASTER,
        storage_uri="logo_raster/key-2",
        sha256="a" * 64,
        source=DiscoverySource.FAVICON,
        confidence=0.5,
    )
    draft = BrandDiscoveryDraft(
        business_id=business_id,
        source_url=None,
        discovered_at=FIXED_UPDATED_AT,
        logo_candidates=(candidate,),
    )
    storage = InMemoryBrandAssetStorage(seed={"logo_raster/key-2": _PNG_BYTES})
    use_case = _use_case(drafts=InMemoryBrandDiscoveryDraftRepository([draft]), storage=storage)

    preview = await use_case.execute(
        BrandAssetPreviewRequest(business_id=business_id, asset_id="candidate-1")
    )

    assert preview.payload == _PNG_BYTES


async def test_raises_when_the_asset_id_belongs_to_a_different_business() -> None:
    owner_business_id = BusinessId.new()
    foreign_business_id = BusinessId.new()
    asset = BrandAsset(
        asset_id="logo-1",
        kind=AssetKind.LOGO_RASTER,
        storage_uri="logo_raster/key-1",
        usage_rule="x",
    )
    kit = make_brand_kit(business_id=owner_business_id, assets=(asset,))
    storage = InMemoryBrandAssetStorage(seed={"logo_raster/key-1": _PNG_BYTES})
    use_case = _use_case(kits=InMemoryBrandKitRepository([kit]), storage=storage)

    with pytest.raises(BrandAssetNotFoundError):
        await use_case.execute(
            BrandAssetPreviewRequest(business_id=foreign_business_id, asset_id="logo-1")
        )


async def test_raises_when_the_asset_id_does_not_exist_at_all() -> None:
    use_case = _use_case()

    with pytest.raises(BrandAssetNotFoundError):
        await use_case.execute(
            BrandAssetPreviewRequest(business_id=BusinessId.new(), asset_id="missing")
        )
