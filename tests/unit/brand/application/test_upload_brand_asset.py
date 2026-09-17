"""`UploadBrandAsset`: dos caminos, sin bytes libres via MCP.
`from_bytes` (panel, multipart) almacena y anade al borrador;
`from_existing_candidate` (MCP) solo reclasifica un `asset_id` que ya
esta en el borrador -- nunca bytes ni URLs nuevas."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.brand.application.errors import BrandDraftAssetNotFoundError
from safent_ads.brand.application.upload_brand_asset import (
    SelectExistingBrandAssetRequest,
    UploadBrandAsset,
    UploadBrandAssetBytesRequest,
)
from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft, DiscoverySource, LogoCandidate
from safent_ads.brand.testing.in_memory_brand_asset_storage import InMemoryBrandAssetStorage
from safent_ads.brand.testing.in_memory_brand_discovery_draft_repository import (
    InMemoryBrandDiscoveryDraftRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _use_case() -> tuple[UploadBrandAsset, InMemoryBrandDiscoveryDraftRepository]:
    drafts = InMemoryBrandDiscoveryDraftRepository()
    use_case = UploadBrandAsset(
        drafts=drafts, storage=InMemoryBrandAssetStorage(), clock=FixedClock(_NOW)
    )
    return use_case, drafts


async def test_from_bytes_stores_the_payload_and_creates_a_draft_if_none_exists() -> None:
    business_id = BusinessId.new()
    use_case, drafts = _use_case()

    candidate = await use_case.from_bytes(
        UploadBrandAssetBytesRequest(
            business_id=business_id, kind=AssetKind.LOGO_RASTER, payload=_PNG_BYTES
        )
    )

    assert candidate.source == DiscoverySource.MANUAL_UPLOAD
    assert candidate.confidence == 1.0
    draft = await drafts.get_by_business(business_id)
    assert draft is not None
    assert draft.logo_by_asset_id(candidate.asset_id) == candidate


async def test_from_bytes_appends_to_an_existing_draft_without_losing_prior_candidates() -> None:
    business_id = BusinessId.new()
    existing_logo = LogoCandidate(
        asset_id="scraped-1",
        kind=AssetKind.ICON,
        storage_uri="brand/scraped.png",
        sha256="a" * 64,
        source=DiscoverySource.FAVICON,
        confidence=0.3,
    )
    draft = BrandDiscoveryDraft(
        business_id=business_id,
        source_url="https://example-business.test",
        discovered_at=_NOW,
        logo_candidates=(existing_logo,),
    )
    use_case, drafts = _use_case()
    await drafts.save(draft)

    await use_case.from_bytes(
        UploadBrandAssetBytesRequest(
            business_id=business_id, kind=AssetKind.LOGO_RASTER, payload=_PNG_BYTES
        )
    )

    updated = await drafts.get_by_business(business_id)
    assert updated is not None
    assert updated.logo_by_asset_id("scraped-1") == existing_logo
    assert len(updated.logo_candidates) == 2


async def test_from_existing_candidate_reclassifies_kind_without_moving_bytes() -> None:
    business_id = BusinessId.new()
    scraped = LogoCandidate(
        asset_id="scraped-1",
        kind=AssetKind.ICON,
        storage_uri="brand/scraped.png",
        sha256="a" * 64,
        source=DiscoverySource.FAVICON,
        confidence=0.3,
    )
    draft = BrandDiscoveryDraft(
        business_id=business_id, source_url=None, discovered_at=_NOW, logo_candidates=(scraped,)
    )
    use_case, drafts = _use_case()
    await drafts.save(draft)

    reclassified = await use_case.from_existing_candidate(
        SelectExistingBrandAssetRequest(
            business_id=business_id, asset_id="scraped-1", kind=AssetKind.LOGO_RASTER
        )
    )

    assert reclassified.kind == AssetKind.LOGO_RASTER
    assert reclassified.storage_uri == "brand/scraped.png"
    updated = await drafts.get_by_business(business_id)
    assert updated.logo_by_asset_id("scraped-1").kind == AssetKind.LOGO_RASTER


async def test_from_existing_candidate_raises_when_asset_id_is_unknown() -> None:
    business_id = BusinessId.new()
    use_case, _drafts = _use_case()

    with pytest.raises(BrandDraftAssetNotFoundError):
        await use_case.from_existing_candidate(
            SelectExistingBrandAssetRequest(
                business_id=business_id, asset_id="missing", kind=AssetKind.LOGO_RASTER
            )
        )
