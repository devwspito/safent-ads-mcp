"""`CatalogReadPort` real (integracion, wiring2) sobre `offerings`/
`calendar_events` (`0025_vocabulary`, tablas renombradas en
`0005_catalog_crm`). `catalog` tiene su propio agregado de escritura
(`catalog.domain.calendar_event.CalendarEvent`) pero este puerto sigue
escribiendo la proyeccion de lectura de MCP directamente contra las
tablas, mismo patron que `panel.infrastructure.sql_read_model` usa para
sus propios DTOs (consulta ad-hoc por contexto en vez de forzar el
agregado de escritura a la forma de lectura).

`is_window_open` nunca se guarda -- se deriva aqui con el mismo `Clock`
inyectable que el resto de la integracion, igual que
`catalog.domain.calendar_event.CalendarEvent.is_window_open`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp.application.dto import (
    CalendarEventDetail,
    CalendarEventSummary,
    OfferingSummary,
)
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.read_models.dto import Money

_SELECT_OFFERINGS = text("""
    SELECT id, title, price_amount, price_currency
      FROM offerings
     WHERE business_id = :business_id AND is_active = true
     ORDER BY title
""")

_SELECT_CALENDAR_EVENTS = text("""
    SELECT id, offering_id, name, kind, window_start, window_end
      FROM calendar_events
     WHERE business_id = :business_id
     ORDER BY window_end
""")

_SELECT_CALENDAR_EVENT = text("""
    SELECT id, offering_id, name, kind, window_start, window_end, event_date, region
      FROM calendar_events
     WHERE business_id = :business_id AND id = :calendar_event_id
""")


class SqlCatalogReadPort:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], clock: Clock | None = None
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    async def list_offerings(self, business_id: str) -> list[OfferingSummary]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(_SELECT_OFFERINGS, {"business_id": business_id})
            ).mappings().all()
        return [_offering(row) for row in rows]

    async def list_calendar_events(
        self, business_id: str, *, open_only: bool
    ) -> list[CalendarEventSummary]:
        today = self._clock.now().date()
        async with self._session_factory() as session:
            rows = (
                await session.execute(_SELECT_CALENDAR_EVENTS, {"business_id": business_id})
            ).mappings().all()
        summaries = [_calendar_event_summary(row, today) for row in rows]
        if open_only:
            return [summary for summary in summaries if summary.is_window_open]
        return summaries

    async def get_calendar_event(
        self, business_id: str, calendar_event_id: str
    ) -> CalendarEventDetail:
        today = self._clock.now().date()
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    _SELECT_CALENDAR_EVENT,
                    {"business_id": business_id, "calendar_event_id": calendar_event_id},
                )
            ).mappings().one_or_none()
        if row is None:
            raise EntityNotFoundError(f"{calendar_event_id} aun no disponible")
        return CalendarEventDetail(
            summary=_calendar_event_summary(row, today),
            window_start=row["window_start"],
            window_end=row["window_end"],
            event_date=row["event_date"],
            region=row["region"],
        )


def _offering(row: RowMapping) -> OfferingSummary:
    price = None
    if row["price_amount"] is not None:
        price = Money(Decimal(row["price_amount"]), row["price_currency"])
    return OfferingSummary(offering_id=str(row["id"]), name=row["title"], price=price)


def _calendar_event_summary(row: RowMapping, today: date) -> CalendarEventSummary:
    return CalendarEventSummary(
        calendar_event_id=str(row["id"]),
        offering_id=str(row["offering_id"]) if row["offering_id"] is not None else "",
        name=row["name"],
        kind=row["kind"],
        is_window_open=row["window_start"] <= today <= row["window_end"],
    )
