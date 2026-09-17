"""`BrandMcpTools`: manejadores puros pydantic-estrictos (threat-model.md
C-11). No hay `ToolRegistry` aqui -- se prueban como funciones normales
(mismo patron que `creative.presentation.mcp_tools`).

`ingest_brand_from_website` (F-8): nunca acepta `url` -- solo rastrea
`BrandKit.confirmed_website_host`, y reporta
`WebsiteDomainNotConfirmedResult` si el negocio no tiene ninguno."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from safent_ads.brand.application.confirm_brand_draft import ConfirmBrandDraft
from safent_ads.brand.application.errors import (
    BrandDraftAssetNotFoundError,
    BrandDraftNotFoundError,
)
from safent_ads.brand.application.get_brand_draft import GetBrandDraft
from safent_ads.brand.application.get_brand_kit import GetBrandKit
from safent_ads.brand.application.ingest_brand_from_website import IngestBrandFromWebsite
from safent_ads.brand.application.upload_brand_asset import UploadBrandAsset
from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.domain.discovery import BrandDiscoveryDraft, DiscoverySource, LogoCandidate
from safent_ads.brand.presentation.mcp_tools import (
    BrandMcpTools,
    ConfirmBrandDraftArgs,
    GetBrandDraftArgs,
    IngestBrandFromWebsiteArgs,
    IngestBrandFromWebsiteResult,
    UploadBrandAssetArgs,
    WebsiteDomainNotConfirmedResult,
)
from safent_ads.brand.testing.in_memory_brand_asset_storage import InMemoryBrandAssetStorage
from safent_ads.brand.testing.in_memory_brand_discovery_draft_repository import (
    InMemoryBrandDiscoveryDraftRepository,
)
from safent_ads.brand.testing.in_memory_brand_kit_repository import InMemoryBrandKitRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId
from tests.unit.brand.factories import make_brand_kit

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

_LOGO = LogoCandidate(
    asset_id="logo-1",
    kind=AssetKind.LOGO_RASTER,
    storage_uri="brand/logo-1.png",
    sha256="a" * 64,
    source=DiscoverySource.OG_IMAGE,
    confidence=0.7,
)


class _FakeWebsiteBrandDiscovery:
    def __init__(self, draft: BrandDiscoveryDraft) -> None:
        self._draft = draft
        self.calls: list[tuple[BusinessId, str]] = []

    async def discover(self, business_id: BusinessId, url: str) -> BrandDiscoveryDraft:
        self.calls.append((business_id, url))
        return self._draft


def _tools(
    *,
    drafts: InMemoryBrandDiscoveryDraftRepository | None = None,
    kits: InMemoryBrandKitRepository | None = None,
    discovery_draft: BrandDiscoveryDraft | None = None,
) -> tuple[BrandMcpTools, _FakeWebsiteBrandDiscovery]:
    drafts = drafts or InMemoryBrandDiscoveryDraftRepository()
    kits = kits or InMemoryBrandKitRepository()
    clock = FixedClock(_NOW)
    business_id = BusinessId.new()
    discovery = _FakeWebsiteBrandDiscovery(
        discovery_draft
        or BrandDiscoveryDraft(business_id=business_id, source_url=None, discovered_at=_NOW)
    )
    tools = BrandMcpTools(
        ingest_brand_from_website=IngestBrandFromWebsite(
            discovery=discovery, drafts=drafts, brand_kits=kits, clock=clock
        ),
        get_brand_kit=GetBrandKit(kits),
        get_brand_draft=GetBrandDraft(drafts),
        confirm_brand_draft=ConfirmBrandDraft(drafts=drafts, brand_kits=kits, clock=clock),
        upload_brand_asset=UploadBrandAsset(
            drafts=drafts, storage=InMemoryBrandAssetStorage(), clock=clock
        ),
    )
    return tools, discovery


def test_ingest_brand_from_website_args_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        IngestBrandFromWebsiteArgs(
            business_id=str(BusinessId.new()),
            url="https://example-business.test",  # type: ignore[call-arg]
        )


async def test_ingest_brand_from_website_reports_when_domain_is_not_confirmed() -> None:
    tools, discovery = _tools()

    result = await tools.ingest_brand_from_website(
        IngestBrandFromWebsiteArgs(business_id=str(BusinessId.new()))
    )

    assert isinstance(result, WebsiteDomainNotConfirmedResult)
    assert result.domain_confirmed is False
    assert discovery.calls == []


async def test_ingest_brand_from_website_reports_when_brand_kit_has_no_host() -> None:
    business_id = BusinessId.new()
    kits = InMemoryBrandKitRepository(
        [make_brand_kit(business_id=business_id, confirmed_website_host=None)]
    )
    tools, discovery = _tools(kits=kits)

    result = await tools.ingest_brand_from_website(
        IngestBrandFromWebsiteArgs(business_id=str(business_id))
    )

    assert isinstance(result, WebsiteDomainNotConfirmedResult)
    assert discovery.calls == []


async def test_ingest_brand_from_website_crawls_the_confirmed_host_only() -> None:
    business_id = BusinessId.new()
    kits = InMemoryBrandKitRepository(
        [make_brand_kit(business_id=business_id, confirmed_website_host="example-business.test")]
    )
    draft = BrandDiscoveryDraft(
        business_id=business_id,
        source_url="https://example-business.test/",
        discovered_at=_NOW,
        logo_candidates=(_LOGO,),
    )
    tools, discovery = _tools(kits=kits, discovery_draft=draft)

    result = await tools.ingest_brand_from_website(
        IngestBrandFromWebsiteArgs(business_id=str(business_id))
    )

    assert isinstance(result, IngestBrandFromWebsiteResult)
    assert result.domain_confirmed is True
    assert result.logo_candidate_count == 1
    assert discovery.calls == [(business_id, "https://example-business.test/")]


async def test_get_brand_draft_returns_the_stored_draft() -> None:
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(business_id=business_id, source_url=None, discovered_at=_NOW)
    tools, _discovery = _tools(drafts=InMemoryBrandDiscoveryDraftRepository([draft]))

    result = await tools.get_brand_draft(GetBrandDraftArgs(business_id=str(business_id)))

    assert result["business_id"] == str(business_id)


async def test_get_brand_draft_raises_when_missing() -> None:
    tools, _discovery = _tools()

    with pytest.raises(BrandDraftNotFoundError):
        await tools.get_brand_draft(GetBrandDraftArgs(business_id=str(BusinessId.new())))


async def test_confirm_brand_draft_produces_an_is_confirmed_kit() -> None:
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(
        business_id=business_id, source_url=None, discovered_at=_NOW, logo_candidates=(_LOGO,)
    )
    tools, _discovery = _tools(drafts=InMemoryBrandDiscoveryDraftRepository([draft]))

    result = await tools.confirm_brand_draft(
        ConfirmBrandDraftArgs(
            business_id=str(business_id),
            primary_font="Poppins",
            font_licence_note="Google Fonts, SIL OFL 1.1",
            tone_description="Cercano y claro.",
            selected_asset_ids=("logo-1",),
        )
    )

    assert result["is_confirmed"] is True
    assert result["typography"]["primary_family"] == "Poppins"


async def test_confirm_brand_draft_raises_when_no_draft_exists() -> None:
    tools, _discovery = _tools()

    with pytest.raises(BrandDraftNotFoundError):
        await tools.confirm_brand_draft(
            ConfirmBrandDraftArgs(
                business_id=str(BusinessId.new()),
                primary_font="Poppins",
                font_licence_note="Google Fonts, SIL OFL 1.1",
                tone_description="Cercano y claro.",
            )
        )


async def test_upload_brand_asset_reclassifies_an_existing_candidate() -> None:
    business_id = BusinessId.new()
    draft = BrandDiscoveryDraft(
        business_id=business_id, source_url=None, discovered_at=_NOW, logo_candidates=(_LOGO,)
    )
    tools, _discovery = _tools(drafts=InMemoryBrandDiscoveryDraftRepository([draft]))

    result = await tools.upload_brand_asset(
        UploadBrandAssetArgs(
            business_id=str(business_id), asset_id="logo-1", kind=AssetKind.ICON
        )
    )

    assert result["kind"] == "icon"
    assert result["asset_id"] == "logo-1"


async def test_upload_brand_asset_raises_when_asset_id_is_unknown() -> None:
    tools, _discovery = _tools()

    with pytest.raises(BrandDraftAssetNotFoundError):
        await tools.upload_brand_asset(
            UploadBrandAssetArgs(
                business_id=str(BusinessId.new()), asset_id="missing", kind=AssetKind.ICON
            )
        )


def test_upload_brand_asset_args_rejects_path_traversal_asset_id() -> None:
    with pytest.raises(ValidationError):
        UploadBrandAssetArgs(
            business_id=str(BusinessId.new()), asset_id="../../etc/passwd", kind=AssetKind.ICON
        )
