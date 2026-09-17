"""`ActiveBusinessIdsPort` real sobre `businesses`: unica consulta de "los
negocios activos" del despliegue, compartida por los resolutores de alcance
que la necesitan -- nunca copiada en cada uno."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SELECT_ACTIVE_BUSINESS_IDS = text("SELECT id FROM businesses WHERE is_active = true")


class SqlActiveBusinessIds:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def active_business_ids(self) -> frozenset[str]:
        async with self._session_factory() as session:
            rows = (await session.execute(_SELECT_ACTIVE_BUSINESS_IDS)).all()
        return frozenset(str(row.id) for row in rows)
