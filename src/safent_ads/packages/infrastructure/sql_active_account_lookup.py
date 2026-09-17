"""`SqlActiveAccountLookup`/`SqlAccountDailyCap`: implementan
`ActiveAccountLookupPort`/`AccountDailyCapPort` sobre `platform_accounts`/
`guardrails` por SQL directo -- NUNCA importando
`opportunities.infrastructure.sql_repositories` (ese contexto no esta en
`packages -> {proposals, execution, creative, accounts, shared}`, y
`opportunities` tampoco importa `packages`: mismo criterio documentado ya
en ese modulo -- "esta lane no tiene autorizado anadir ficheros a
composition/" -- aplicado en sentido inverso aqui."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.packages.application.ports import AmbiguousActiveAccountForPlatformError
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

__all__ = ["SqlAccountDailyCap", "SqlActiveAccountLookup"]

_FIND_ACTIVE_ACCOUNT: Final = text("""
    SELECT platform, external_account_id, business_id, connection_id FROM platform_accounts
     WHERE business_id = :business_id AND platform = :platform AND status = 'ACTIVE'
       AND (CAST(:account_ref AS TEXT) IS NULL OR account_ref = :account_ref)
     ORDER BY created_at
     LIMIT 2
""")

_FIND_ACCOUNT_DAILY_CAP: Final = text("""
    SELECT g.daily_cap_minor, g.currency
      FROM guardrails AS g
      JOIN platform_accounts AS pa ON pa.id = g.platform_account_id
     WHERE g.scope = 'platform_account'
       AND pa.account_ref = :account_ref
       AND g.daily_cap_minor IS NOT NULL
     LIMIT 1
""")

_MINOR_UNITS_PER_MAJOR: Final = 100


class SqlActiveAccountLookup:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_active_account(
        self,
        *,
        business_id: BusinessId,
        platform: PlatformCode,
        account_ref: EntityRef | None = None,
    ) -> EntityRef | None:
        rows = (
            (
                await self._session.execute(
                    _FIND_ACTIVE_ACCOUNT,
                    {
                        "business_id": business_id.value,
                        "platform": platform.value,
                        "account_ref": str(account_ref) if account_ref else None,
                    },
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return None
        if len(rows) > 1:
            raise AmbiguousActiveAccountForPlatformError(
                f"{business_id.value}/{platform.value}: multiple ACTIVE accounts"
            )
        row = rows[0]
        return EntityRef(
            platform=PlatformCode(row["platform"]),
            level=EntityLevel.ACCOUNT,
            external_id=row["external_account_id"],
            business_id=row["business_id"] if row["connection_id"] else None,
            connection_id=row["connection_id"],
        )


class SqlAccountDailyCap:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_daily_cap(self, *, account_ref: EntityRef) -> Money | None:
        result = await self._session.execute(
            _FIND_ACCOUNT_DAILY_CAP, {"account_ref": str(account_ref)}
        )
        row = result.mappings().first()
        if row is None:
            return None
        major_amount = Decimal(row["daily_cap_minor"]) / _MINOR_UNITS_PER_MAJOR
        return Money.of(major_amount, row["currency"])
