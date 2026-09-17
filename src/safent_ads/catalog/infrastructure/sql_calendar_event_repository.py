"""`CalendarEventRepository` sobre `calendar_events`
(`0025_vocabulary`, tabla renombrada en `0005_catalog_crm`).

`source`/`source_url` (antes `fuente`/`fuente_url`) son procedencia
interna -- ninguna herramienta MCP ni el contrato REST de esta lane los
expone (`contracts/rest-api.md §Calendario`): todo evento creado por este
repositorio nace con `source='panel'`, `source_url=NULL`, y ninguna
escritura los vuelve a tocar (Assumption documentada, tech-lead)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.catalog.application.errors import CalendarEventNotFoundError
from safent_ads.catalog.domain.calendar_event import (
    CalendarEvent,
    CalendarEventId,
    CalendarEventKind,
)
from safent_ads.catalog.infrastructure.errors import OfferingNotFoundError
from safent_ads.shared.ids import BusinessId

__all__ = ["SqlCalendarEventRepository"]

_INSERT = text("""
    INSERT INTO calendar_events
        (id, business_id, offering_id, name, region, kind, window_start, window_end,
         event_date, source)
    VALUES
        (:id, :business_id, :offering_id, :name, :region, :kind, :window_start, :window_end,
         :event_date, 'panel')
    RETURNING id, business_id, offering_id, name, region, kind, window_start, window_end,
              event_date
""")

_UPDATE = text("""
    UPDATE calendar_events
       SET offering_id = :offering_id, name = :name, region = :region, kind = :kind,
           window_start = :window_start, window_end = :window_end, event_date = :event_date
     WHERE id = :id AND business_id = :business_id
    RETURNING id, business_id, offering_id, name, region, kind, window_start, window_end,
              event_date
""")

_DELETE = text(
    "DELETE FROM calendar_events WHERE id = :id AND business_id = :business_id RETURNING id"
)

_GET = text("""
    SELECT id, business_id, offering_id, name, region, kind, window_start, window_end, event_date
      FROM calendar_events
     WHERE id = :id AND business_id = :business_id
""")

_LIST = text("""
    SELECT id, business_id, offering_id, name, region, kind, window_start, window_end, event_date
      FROM calendar_events
     WHERE business_id = :business_id
       AND (CAST(:kind AS TEXT) IS NULL OR kind IS NOT DISTINCT FROM :kind)
     ORDER BY window_end
""")

_OFFERING_EXISTS = text(
    "SELECT 1 FROM offerings WHERE id = :offering_id AND business_id = :business_id"
)


class SqlCalendarEventRepository:
    """Una `AsyncSession`, no comete `commit()` -- el limite de la
    transaccion lo pone quien la abre (mismo criterio que
    `SqlSettingsRepository`/`SqlProposalRepository`)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, event: CalendarEvent) -> CalendarEvent:
        await self._require_offering_exists(event.offering_id, event.business_id)
        result = await self._session.execute(_INSERT, _event_params(event))
        await self._session.flush()
        return _to_domain(result.mappings().one())

    async def update(self, event: CalendarEvent) -> CalendarEvent:
        await self._require_offering_exists(event.offering_id, event.business_id)
        result = await self._session.execute(_UPDATE, _event_params(event))
        row = result.mappings().one_or_none()
        await self._session.flush()
        if row is None:
            raise CalendarEventNotFoundError(str(event.calendar_event_id))
        return _to_domain(row)

    async def delete(self, calendar_event_id: CalendarEventId, business_id: BusinessId) -> bool:
        result = await self._session.execute(
            _DELETE, {"id": str(calendar_event_id.value), "business_id": str(business_id.value)}
        )
        deleted = result.mappings().one_or_none() is not None
        await self._session.flush()
        return deleted

    async def get(
        self, calendar_event_id: CalendarEventId, business_id: BusinessId
    ) -> CalendarEvent | None:
        result = await self._session.execute(
            _GET, {"id": str(calendar_event_id.value), "business_id": str(business_id.value)}
        )
        row = result.mappings().one_or_none()
        return None if row is None else _to_domain(row)

    async def list_for_business(
        self, business_id: BusinessId, *, kind: str | None
    ) -> list[CalendarEvent]:
        result = await self._session.execute(
            _LIST, {"business_id": str(business_id.value), "kind": kind}
        )
        return [_to_domain(row) for row in result.mappings()]

    async def _require_offering_exists(
        self, offering_id: str | None, business_id: BusinessId
    ) -> None:
        if offering_id is None:
            return
        result = await self._session.execute(
            _OFFERING_EXISTS, {"offering_id": offering_id, "business_id": str(business_id.value)}
        )
        if result.one_or_none() is None:
            raise OfferingNotFoundError(f"offering_id {offering_id} no encontrado")


def _event_params(event: CalendarEvent) -> dict[str, object]:
    return {
        "id": str(event.calendar_event_id.value),
        "business_id": str(event.business_id.value),
        "offering_id": event.offering_id,
        "name": event.name,
        "region": event.region,
        "kind": event.kind.value,
        "window_start": event.window_start,
        "window_end": event.window_end,
        "event_date": event.event_date,
    }


def _to_domain(row: RowMapping) -> CalendarEvent:
    return CalendarEvent(
        calendar_event_id=CalendarEventId.parse(str(row["id"])),
        business_id=BusinessId.parse(str(row["business_id"])),
        name=row["name"],
        kind=CalendarEventKind(row["kind"]),
        window_start=row["window_start"],
        window_end=row["window_end"],
        offering_id=str(row["offering_id"]) if row["offering_id"] is not None else None,
        region=row["region"],
        event_date=row["event_date"],
    )
