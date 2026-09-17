"""`SqlBrandReadPort` (mcp/infrastructure): mismo agregado que
`SqlBrandKitRepository` (`0014_brand.py`), traducido a los DTOs tipados de
`mcp.application.dto` en vez del `dict` que usa el router REST.

`httpx.ASGITransport`/`rolled_back_session` no sirven aqui: este puerto
abre su PROPIA sesion por llamada (`session_factory()`), asi que sembrar
con una sesion de savepoint (`tests.conftest.rolled_back_session`) la
dejaria invisible para la sesion que el puerto abre (misma razon que
documenta `tests/integration/panel/test_authorization_integration.py`).
Motor y commit real, con limpieza explicita al terminar."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.unit.brand.factories import make_brand_kit

from safent_ads.brand.infrastructure.sql_brand_kit_repository import SqlBrandKitRepository
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.sql_brand_read_port import SqlBrandReadPort
from safent_ads.shared.ids import BusinessId

pytestmark = pytest.mark.integration


@pytest.fixture
async def committed_business(
    database_url: str,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], uuid.UUID]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    business_id = uuid.uuid4()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, :name, 'Europe/Madrid', 'EUR')"
            ),
            {
                "id": str(business_id),
                "slug": f"fixture-{business_id.hex[:8]}",
                "name": "Fixture Business",
            },
        )
        await session.commit()
    try:
        yield factory, business_id
    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM brand_kits WHERE business_id = :id"), {"id": str(business_id)}
            )
            await session.execute(
                text("DELETE FROM businesses WHERE id = :id"), {"id": str(business_id)}
            )
            await session.commit()
        await engine.dispose()


async def test_get_brand_kit_maps_the_full_aggregate_to_the_mcp_dto(
    committed_business: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    factory, business_id = committed_business
    kit = make_brand_kit(business_id=BusinessId.parse(str(business_id)))
    async with factory() as session:
        await SqlBrandKitRepository(session).save(kit)
        await session.commit()

    detail = await SqlBrandReadPort(factory).get_brand_kit(str(business_id))

    assert detail.business_id == str(business_id)
    assert detail.typography.primary_family == kit.typography.primary_family
    assert detail.palette[0].hex == kit.palette.swatches[0].hex
    assert detail.palette[0].meets_wcag_aa_normal_text == (
        kit.palette.swatches[0].meets_wcag_aa_normal_text()
    )
    assert detail.tone_of_voice.description == kit.tone_of_voice.description
    assert detail.assets[0].asset_id == kit.assets[0].asset_id
    assert detail.assets[0].url == kit.assets[0].storage_uri
    assert detail.legal_disclaimers[0].text == kit.legal_disclaimers[0].text
    assert detail.is_complete == kit.is_complete()


async def test_get_brand_kit_raises_entity_not_found_when_business_has_no_kit(
    committed_business: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    factory, business_id = committed_business

    with pytest.raises(EntityNotFoundError):
        await SqlBrandReadPort(factory).get_brand_kit(str(business_id))


async def test_list_brand_assets_filters_by_kind(
    committed_business: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    factory, business_id = committed_business
    kit = make_brand_kit(business_id=BusinessId.parse(str(business_id)))
    async with factory() as session:
        await SqlBrandKitRepository(session).save(kit)
        await session.commit()

    assets = await SqlBrandReadPort(factory).list_brand_assets(
        str(business_id), kind=kit.assets[0].kind.value
    )

    assert [a.asset_id for a in assets] == [kit.assets[0].asset_id]


async def test_list_brand_assets_is_empty_when_business_has_no_kit(
    committed_business: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    factory, business_id = committed_business

    assets = await SqlBrandReadPort(factory).list_brand_assets(str(business_id), kind=None)

    assert assets == []
