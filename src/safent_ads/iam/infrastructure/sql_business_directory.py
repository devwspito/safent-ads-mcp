"""Adaptador SQL de `BusinessDirectory` sobre `businesses`
(0001_bootstrap.py). Lectura minima -- existencia e id/slug/name -- para
`require_business_access` (C-27) y `/auth/me`; no es el contexto
`accounts` (agregados `Business`/`PlatformAccount` completos), fuera del
alcance de este carril. Modelo de propietario unico (data-model.md): no
hay tabla de asignacion propietario-negocio, "poseer" un negocio es que la
fila exista."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_EXISTS_SQL = text("SELECT 1 FROM businesses WHERE id = :id")
_LIST_ALL_SQL = text("SELECT id, slug, name FROM businesses ORDER BY name")


class SqlBusinessDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, business_id: uuid.UUID) -> bool:
        result = await self._session.execute(_EXISTS_SQL, {"id": str(business_id)})
        return result.one_or_none() is not None

    async def list_all(self) -> tuple[tuple[uuid.UUID, str, str], ...]:
        result = await self._session.execute(_LIST_ALL_SQL)
        return tuple((row.id, row.slug, row.name) for row in result.all())
