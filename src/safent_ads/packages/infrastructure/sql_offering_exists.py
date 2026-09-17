"""`SqlOfferingExists`: implementa `OfferingExistsPort` sobre `offerings`
por SQL directo -- `catalog` no esta en `packages -> {proposals, execution,
creative, accounts, shared}` (es fuente de solo lectura para el agente,
data-model.md "Bounded contexts")."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.shared.ids import BusinessId

__all__ = ["SqlOfferingExists"]

_EXISTS_SQL = text(
    "SELECT 1 FROM offerings WHERE business_id = :business_id AND id = :offering_id "
    "AND is_active = true"
)


class SqlOfferingExists:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, *, business_id: BusinessId, offering_id: str) -> bool:
        try:
            offering_uuid = uuid.UUID(offering_id)
        except ValueError:
            return False
        result = await self._session.execute(
            _EXISTS_SQL, {"business_id": business_id.value, "offering_id": offering_uuid}
        )
        return result.first() is not None
