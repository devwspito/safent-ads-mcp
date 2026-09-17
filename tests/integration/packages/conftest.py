"""Siembra minima real (Postgres, `db_session`) para los tests de
`packages.infrastructure` contra la migracion 0042/0044: negocio + cuenta
CONECTADA (con `connection_id`, para que `platform_accounts.account_ref`
generado coincida byte a byte con `EntityRef.__str__()`, que
`campaign_package.CampaignPackage.propose` exige con alcance) + oferta."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode


@dataclass(frozen=True, slots=True)
class SeededScope:
    business_id: BusinessId
    account_ref: EntityRef
    offering_id: str


async def seed_package_scope(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    business_id: uuid.UUID,
    platform: PlatformCode = PlatformCode.META,
    external_account_id: str | None = None,
) -> SeededScope:
    connection_id = uuid.uuid4()
    account_id = uuid.uuid4()
    offering_id = uuid.uuid4()
    external_account_id = external_account_id or f"act-{uuid.uuid4().hex[:8]}"

    await session.execute(
        text(
            "INSERT INTO platform_connections (id, business_id, owner_id, platform) "
            "VALUES (:id, :business_id, :owner_id, :platform)"
        ),
        {
            "id": connection_id,
            "business_id": business_id,
            "owner_id": owner_id,
            "platform": platform.value,
        },
    )
    await session.execute(
        text(
            "INSERT INTO platform_accounts "
            "(id, business_id, platform, external_account_id, currency, timezone, "
            " api_tier, status, connection_id) "
            "VALUES (:id, :business_id, :platform, :external_account_id, 'EUR', "
            "'Europe/Madrid', 'meta_full', 'ACTIVE', :connection_id)"
        ),
        {
            "id": account_id,
            "business_id": business_id,
            "platform": platform.value,
            "external_account_id": external_account_id,
            "connection_id": connection_id,
        },
    )
    await session.execute(
        text(
            "INSERT INTO offerings (id, business_id, code, title) "
            "VALUES (:id, :business_id, :code, 'Reserva de citas')"
        ),
        {"id": offering_id, "business_id": business_id, "code": f"of-{uuid.uuid4().hex[:8]}"},
    )
    await session.flush()

    account_ref = EntityRef(
        platform=platform,
        level=EntityLevel.ACCOUNT,
        external_id=external_account_id,
        business_id=business_id,
        connection_id=connection_id,
    )
    return SeededScope(
        business_id=BusinessId.parse(str(business_id)),
        account_ref=account_ref,
        offering_id=str(offering_id),
    )
