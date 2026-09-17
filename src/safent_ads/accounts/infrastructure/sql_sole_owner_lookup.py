"""SQL adapter of `SoleOwnerLookupPort` over `owners`.

Same notion of "the sole owner" as the bridge repository (oldest row), but it
refuses to pick one when the table holds zero or several owners.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SELECT_OWNER_IDS_SQL = text("SELECT id FROM owners ORDER BY created_at ASC LIMIT 2")


class SqlSoleOwnerLookup:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def sole_owner_id(self) -> uuid.UUID | None:
        rows = (await self._session.execute(_SELECT_OWNER_IDS_SQL)).all()
        if len(rows) != 1:
            return None
        return uuid.UUID(str(rows[0][0]))
