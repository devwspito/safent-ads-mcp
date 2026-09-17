"""One transaction serializes first entry; no seeds, accounts or spend grants."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.create_initial_business import (
    BusinessAlreadyConfiguredError,
    OwnerConfigurationAmbiguousError,
)
from safent_ads.accounts.domain.business import Business


class SqlInitialBusinessRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_for_sole_owner(self, owner_id: uuid.UUID, business: Business) -> None:
        # A row lock cannot protect the "exactly one owner" predicate from an
        # INSERT. These short local metadata locks serialize initial setup and
        # block concurrent owner additions/deletions until commit. No broker,
        # OAuth, external request or user interaction occurs while locked.
        await self._session.execute(text("LOCK TABLE owners IN SHARE ROW EXCLUSIVE MODE"))
        owner_ids = list((await self._session.execute(text("SELECT id FROM owners"))).scalars())
        if owner_ids != [owner_id]:
            raise OwnerConfigurationAmbiguousError
        await self._session.execute(text("LOCK TABLE businesses IN SHARE ROW EXCLUSIVE MODE"))
        if (await self._session.execute(text("SELECT 1 FROM businesses LIMIT 1"))).first():
            raise BusinessAlreadyConfiguredError
        await self._session.execute(
            text("""INSERT INTO businesses (id, slug, name, timezone, reference_currency)
                    VALUES (:id, :slug, :name, :timezone, :currency)"""),
            {
                "id": business.business_id.value,
                "slug": business.slug,
                "name": business.name,
                "timezone": business.timezone,
                "currency": business.reference_currency,
            },
        )
        # The HTTP transaction owns commit/rollback. A failed or cancelled
        # request cannot leave a partially configured business behind.
