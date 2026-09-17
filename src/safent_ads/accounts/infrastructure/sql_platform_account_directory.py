"""`SqlPlatformAccountDirectory`: implementa `PlatformAccountDirectoryPort`
sobre `platform_accounts` (spec 008 T032).

`external_account_id` guarda ya la forma CANONICA de la cuenta -- el
`customer_id` de Google y el `act_<id>` de Meta, la misma que el broker
resuelve y la misma que viaja en `AccountRef`/`caps.yaml`. Por eso la
comparacion es exacta: `ads-api` no inventa su propia forma del id
(contracts/broker-set-account-caps.schema.json).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.shared.ids import BusinessId

__all__ = ["SqlPlatformAccountDirectory"]

_FIND_BUSINESS: Final = text("""
    SELECT business_id FROM platform_accounts
     WHERE external_account_id = :external_account_id
     ORDER BY created_at
     LIMIT 1
""")


class SqlPlatformAccountDirectory:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def business_of(self, platform_account_id: str) -> BusinessId | None:
        row = (
            await self._session.execute(
                _FIND_BUSINESS, {"external_account_id": platform_account_id}
            )
        ).first()
        return None if row is None else BusinessId(row[0])
