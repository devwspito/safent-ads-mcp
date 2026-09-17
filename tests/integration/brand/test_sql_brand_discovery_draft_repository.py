"""`SqlBrandDiscoveryDraftRepository` contra Postgres real
(0015_brand_discovery.py): el UNIQUE de `business_id` y el `CAST(... AS
JSONB)` de cada lista de candidatos, mismo motivo que
`test_sql_brand_kit_repository.py`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.brand.domain.brand_asset import AssetKind
from safent_ads.brand.domain.color_palette import ColorRole
from safent_ads.brand.domain.discovery import (
    BrandDiscoveryDraft,
    ColorCandidate,
    ContactChannelCandidate,
    ContactChannelKind,
    CopySample,
    DiscoverySource,
    LogoCandidate,
    SocialLinkCandidate,
    SocialNetwork,
    TypographyCandidate,
)
from safent_ads.brand.infrastructure.sql_brand_discovery_draft_repository import (
    SqlBrandDiscoveryDraftRepository,
)
from safent_ads.shared.ids import BusinessId
from tests.conftest import BusinessFactory

pytestmark = pytest.mark.integration

_DISCOVERED_AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _draft(business_id: BusinessId) -> BrandDiscoveryDraft:
    return BrandDiscoveryDraft(
        business_id=business_id,
        source_url="https://example-business.test",
        discovered_at=_DISCOVERED_AT,
        logo_candidates=(
            LogoCandidate(
                asset_id="logo-1",
                kind=AssetKind.LOGO_RASTER,
                storage_uri="brand/logo-1.png",
                sha256="a" * 64,
                source=DiscoverySource.OG_IMAGE,
                confidence=0.7,
            ),
        ),
        color_candidates=(
            ColorCandidate(
                hex="#112233",
                source=DiscoverySource.CSS_CUSTOM_PROPERTY,
                confidence=0.8,
                role_hint=ColorRole.PRIMARY,
            ),
        ),
        typography_candidates=(
            TypographyCandidate(
                family="Poppins", source=DiscoverySource.CSS_FONT_FAMILY, confidence=0.5
            ),
        ),
        social_links=(SocialLinkCandidate(network=SocialNetwork.INSTAGRAM, url="https://ig.test"),),
        contact_channels=(
            ContactChannelCandidate(
                kind=ContactChannelKind.CONTACT_FORM, page_url="https://example-business.test/contacto"
            ),
        ),
        copy_samples=(
            CopySample(source=DiscoverySource.HERO_HEADLINE, text="Prepara tu lanzamiento"),
        ),
    )


async def test_round_trips_a_full_draft(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId.parse(str(await business_factory.create()))
    draft = _draft(business_id)
    repository = SqlBrandDiscoveryDraftRepository(db_session)

    await repository.save(draft)
    reloaded = await repository.get_by_business(business_id)

    assert reloaded is not None
    assert reloaded.source_url == draft.source_url
    assert reloaded.logo_candidates[0].asset_id == "logo-1"
    assert reloaded.color_candidates[0].hex == "#112233"
    assert reloaded.typography_candidates[0].family == "Poppins"
    assert reloaded.social_links[0].network == SocialNetwork.INSTAGRAM
    assert reloaded.contact_channels[0].kind == ContactChannelKind.CONTACT_FORM
    assert reloaded.copy_samples[0].text == "Prepara tu lanzamiento"


async def test_returns_none_for_business_without_a_draft(db_session: AsyncSession) -> None:
    repository = SqlBrandDiscoveryDraftRepository(db_session)

    assert await repository.get_by_business(BusinessId.new()) is None


async def test_save_is_an_upsert_on_business_id(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId.parse(str(await business_factory.create()))
    repository = SqlBrandDiscoveryDraftRepository(db_session)
    await repository.save(_draft(business_id))

    replacement = BrandDiscoveryDraft(
        business_id=business_id, source_url=None, discovered_at=_DISCOVERED_AT
    )
    await repository.save(replacement)

    reloaded = await repository.get_by_business(business_id)

    assert reloaded is not None
    assert reloaded.source_url is None
    assert reloaded.logo_candidates == ()
