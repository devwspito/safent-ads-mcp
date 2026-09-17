"""`CatalogReadPort`: `FakeCatalogReadPort` (`mcp/testing/fakes.py`) contra
`SqlCatalogReadPort` (esta lane), mismo banco de contrato -- parametrizado
como `tests/contracts/rules/conftest.py` (in_memory/sql). `SqlCatalogReadPort`
es la unica implementacion que 404 en id desconocido y que respeta
`open_only` de verdad: esas dos aserciones viven en tests SQL-only al final
del fichero, no en el banco compartido (`FakeCatalogReadPort` nunca falla y
siempre marca `is_window_open=True`, mismo principio que documenta
`tests/contracts/rules/conftest.py` para lo que cada doble SI modela)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.sql_catalog_read_port import SqlCatalogReadPort
from safent_ads.mcp.testing.fakes import BUSINESS_A, FakeCatalogReadPort
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class CatalogFixture:
    port: object
    business_id: str
    calendar_event_id: str


async def _seed_sql_catalog(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    business_id = uuid.uuid4()
    offering_id = uuid.uuid4()
    calendar_event_id = uuid.uuid4()
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio de catalogo', 'Europe/Madrid', 'EUR')"
            ),
            {"id": business_id, "slug": f"cat-{business_id.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO offerings (id, business_id, code, title, price_amount, "
                "price_currency) VALUES (:id, :business_id, :code, 'Búsqueda Marca', "
                "310.00, 'EUR')"
            ),
            {"id": offering_id, "business_id": business_id, "code": f"c-{offering_id.hex[:8]}"},
        )
        await session.execute(
            text(
                "INSERT INTO calendar_events (id, business_id, offering_id, name, "
                "region, window_start, window_end, source) "
                "VALUES (:id, :business_id, :offering_id, 'Lanzamiento 2026', 'Madrid', "
                "'2026-01-01', '2026-12-31', 'panel')"
            ),
            {"id": calendar_event_id, "business_id": business_id, "offering_id": offering_id},
        )
        await session.commit()
    return business_id, offering_id, calendar_event_id


async def _cleanup_sql_catalog(
    factory: async_sessionmaker[AsyncSession], business_id: uuid.UUID
) -> None:
    async with factory() as session:
        await session.execute(
            text("DELETE FROM calendar_events WHERE business_id = :id"), {"id": business_id}
        )
        await session.execute(
            text("DELETE FROM offerings WHERE business_id = :id"), {"id": business_id}
        )
        await session.execute(text("DELETE FROM businesses WHERE id = :id"), {"id": business_id})
        await session.commit()


@pytest.fixture(
    params=[
        pytest.param("fake", id="fake"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def catalog(request: pytest.FixtureRequest) -> AsyncIterator[CatalogFixture]:
    if request.param == "fake":
        yield CatalogFixture(FakeCatalogReadPort(), BUSINESS_A, "evt-1")
        return

    factory: async_sessionmaker[AsyncSession] = request.getfixturevalue("mcp_session_factory")
    business_id, _offering_id, calendar_event_id = await _seed_sql_catalog(factory)
    port = SqlCatalogReadPort(factory, clock=FixedClock(_NOW))
    try:
        yield CatalogFixture(port, str(business_id), str(calendar_event_id))
    finally:
        await _cleanup_sql_catalog(factory, business_id)


async def test_list_offerings_returns_a_named_offering(catalog: CatalogFixture) -> None:
    offerings = await catalog.port.list_offerings(catalog.business_id)

    assert len(offerings) >= 1
    assert offerings[0].name


async def test_list_calendar_events_open_only_returns_only_open_windows(
    catalog: CatalogFixture,
) -> None:
    events = await catalog.port.list_calendar_events(catalog.business_id, open_only=True)

    assert len(events) >= 1
    assert all(e.is_window_open for e in events)


async def test_get_calendar_event_echoes_the_requested_id(catalog: CatalogFixture) -> None:
    detail = await catalog.port.get_calendar_event(catalog.business_id, catalog.calendar_event_id)

    assert detail.summary.calendar_event_id == catalog.calendar_event_id
    assert detail.window_start <= detail.window_end


@pytest.mark.integration
async def test_sql_get_calendar_event_unknown_id_raises_not_found(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_id, _offering_id, _calendar_event_id = await _seed_sql_catalog(mcp_session_factory)
    port = SqlCatalogReadPort(mcp_session_factory, clock=FixedClock(_NOW))
    try:
        with pytest.raises(EntityNotFoundError):
            await port.get_calendar_event(str(business_id), str(uuid.uuid4()))
    finally:
        await _cleanup_sql_catalog(mcp_session_factory, business_id)


@pytest.mark.integration
async def test_sql_list_calendar_events_excludes_closed_windows_when_open_only(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_id, offering_id, _calendar_event_id = await _seed_sql_catalog(mcp_session_factory)
    closed_id = uuid.uuid4()
    async with mcp_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO calendar_events (id, business_id, offering_id, name, "
                "region, window_start, window_end, source) "
                "VALUES (:id, :business_id, :offering_id, 'Evento cerrado', 'Madrid', "
                "'2020-01-01', '2020-02-01', 'panel')"
            ),
            {"id": closed_id, "business_id": business_id, "offering_id": offering_id},
        )
        await session.commit()
    port = SqlCatalogReadPort(mcp_session_factory, clock=FixedClock(_NOW))
    try:
        open_only = await port.list_calendar_events(str(business_id), open_only=True)
        all_events = await port.list_calendar_events(str(business_id), open_only=False)
        assert str(closed_id) not in {e.calendar_event_id for e in open_only}
        assert str(closed_id) in {e.calendar_event_id for e in all_events}
    finally:
        await _cleanup_sql_catalog(mcp_session_factory, business_id)
