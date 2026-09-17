"""`SqlBrandKitRepository` contra Postgres real (0014_brand.py): los
dobles no modelan el UNIQUE de `business_id` ni el `CAST(... AS JSONB)`
(data-model.md §Migration plan, "causa raiz numero uno en oposads")."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.brand.domain.brand_asset import AssetKind, BrandAsset
from safent_ads.brand.domain.claims_policy import DEFAULT_FORBIDDEN_CLAIMS
from safent_ads.brand.infrastructure.sql_brand_kit_repository import SqlBrandKitRepository
from safent_ads.shared.ids import BusinessId
from tests.conftest import BusinessFactory
from tests.unit.brand.factories import make_brand_kit

pytestmark = pytest.mark.integration


async def test_round_trips_a_full_brand_kit(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId.parse(str(await business_factory.create()))
    kit = make_brand_kit(business_id=business_id)
    repository = SqlBrandKitRepository(db_session)

    await repository.save(kit)
    reloaded = await repository.get_by_business(business_id)

    assert reloaded is not None
    assert reloaded.business_id == business_id
    assert reloaded.typography.primary_family == kit.typography.primary_family
    assert reloaded.typography.licence_note == kit.typography.licence_note
    assert reloaded.palette.swatches[0].hex == kit.palette.swatches[0].hex
    assert reloaded.tone_of_voice.description == kit.tone_of_voice.description
    assert reloaded.assets[0].asset_id == kit.assets[0].asset_id
    assert DEFAULT_FORBIDDEN_CLAIMS <= reloaded.forbidden_claims
    assert reloaded.legal_disclaimers[0].text == kit.legal_disclaimers[0].text
    assert reloaded.is_confirmed == kit.is_confirmed


async def test_round_trips_an_unconfirmed_draft_kit(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId.parse(str(await business_factory.create()))
    kit = make_brand_kit(business_id=business_id, is_confirmed=False)
    repository = SqlBrandKitRepository(db_session)

    await repository.save(kit)
    reloaded = await repository.get_by_business(business_id)

    assert reloaded is not None
    assert reloaded.is_confirmed is False
    assert reloaded.is_complete() is False


async def test_returns_none_for_business_without_a_kit(db_session: AsyncSession) -> None:
    repository = SqlBrandKitRepository(db_session)

    assert await repository.get_by_business(BusinessId.new()) is None


async def test_round_trips_confirmed_website_host(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    """F-8: `confirmed_website_host` sobrevive un guardado/relectura real
    -- la herramienta MCP `ingest_brand_from_website` lo relee para saber
    que dominio puede rastrear."""
    business_id = BusinessId.parse(str(await business_factory.create()))
    kit = make_brand_kit(business_id=business_id, confirmed_website_host="example-business.test")
    repository = SqlBrandKitRepository(db_session)

    await repository.save(kit)
    reloaded = await repository.get_by_business(business_id)

    assert reloaded is not None
    assert reloaded.confirmed_website_host == "example-business.test"


async def test_confirmed_website_host_defaults_to_none(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId.parse(str(await business_factory.create()))
    kit = make_brand_kit(business_id=business_id)
    repository = SqlBrandKitRepository(db_session)

    await repository.save(kit)
    reloaded = await repository.get_by_business(business_id)

    assert reloaded is not None
    assert reloaded.confirmed_website_host is None


async def test_save_is_an_upsert_on_business_id(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    business_id = BusinessId.parse(str(await business_factory.create()))
    repository = SqlBrandKitRepository(db_session)
    await repository.save(make_brand_kit(business_id=business_id))

    replacement = make_brand_kit(
        business_id=business_id,
        assets=(
            BrandAsset(
                asset_id="logo-nuevo",
                kind=AssetKind.LOGO_RASTER,
                storage_uri="s3://brand/nuevo.png",
                usage_rule="uso nuevo",
            ),
        ),
    )
    await repository.save(replacement)

    reloaded = await repository.get_by_business(business_id)

    assert reloaded is not None
    assert len(reloaded.assets) == 1
    assert reloaded.assets[0].asset_id == "logo-nuevo"


async def test_one_kit_per_business_is_enforced_by_the_unique_constraint(
    db_session: AsyncSession, business_factory: BusinessFactory
) -> None:
    """`save()` es upsert por diseno (arriba); esto prueba que el UNIQUE
    de `business_id` sigue en la tabla, no solo en el codigo de la
    aplicacion."""
    business_id = await business_factory.create()
    insert_sql = text(
        "INSERT INTO brand_kits (business_id, primary_font, font_licence_note, "
        "tone_description) VALUES (:business_id, :marker, :marker, :marker)"
    )

    await db_session.execute(insert_sql, {"business_id": str(business_id), "marker": "X"})

    with pytest.raises(Exception, match="unique"):
        await db_session.execute(insert_sql, {"business_id": str(business_id), "marker": "Y"})
