"""`BusinessDirectoryPort` real (integracion) sobre `businesses`: la unica
herramienta MCP sin `business_id` en sus argumentos (`list_businesses`),
filtrada aqui por `CallerScope.allowed_business_ids` -- vacio devuelve la
lista vacia, nunca "todos" (contracts/mcp.md §4)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp.application.dto import BusinessSummary

_SELECT_BUSINESSES = text(
    "SELECT id, name, timezone, reference_currency FROM businesses "
    "WHERE is_active = true ORDER BY name"
)


class SqlBusinessDirectory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_businesses(self, allowed_business_ids: frozenset[str]) -> list[BusinessSummary]:
        async with self._session_factory() as session:
            rows = (await session.execute(_SELECT_BUSINESSES)).all()
        return [
            BusinessSummary(
                business_id=str(row.id),
                name=row.name,
                timezone=row.timezone,
                currency=row.reference_currency,
            )
            for row in rows
            if str(row.id) in allowed_business_ids
        ]
