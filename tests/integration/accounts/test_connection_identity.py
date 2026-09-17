from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.platform_account import ApiTier, PlatformAccount
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.accounts.infrastructure.sql_repositories import (
    SqlAccountRepository,
    SqlAdEntityRepository,
)
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from tests.conftest import BusinessFactory, OwnerFactory

pytestmark = pytest.mark.integration


async def test_same_remote_ids_are_isolated_by_business_and_connection(
    db_session: AsyncSession,
    business_factory: BusinessFactory,
    owner_factory: OwnerFactory,
) -> None:
    business = await business_factory.create()
    other_business = await business_factory.create()
    owner = await owner_factory.create()
    accounts, entities = SqlAccountRepository(db_session), SqlAdEntityRepository(db_session)
    stored_accounts = []
    for index, business_id in enumerate((business, business, other_business)):
        connection, credential = uuid4(), uuid4()
        await db_session.execute(
            text("""INSERT INTO platform_connections(id,business_id,owner_id,platform)
            VALUES(:connection,:business,:owner,'google')"""),
            {"connection": connection, "business": business_id, "owner": owner},
        )
        await db_session.execute(
            text("""INSERT INTO credential_refs(id,platform,alias)
            VALUES(:id,'google',:alias)"""),
            {"id": credential, "alias": str(credential)},
        )
        account = PlatformAccount(
            BusinessId(business_id),
            AccountRef(PlatformCode.GOOGLE, "123", business_id, connection),
            "EUR",
            "Europe/Madrid",
            ApiTier.GOOGLE_EXPLORER,
            CredentialRefId(credential),
        )
        await accounts.save(account)
        stored_accounts.append(account)
        ref = EntityRef(
            PlatformCode.GOOGLE,
            EntityLevel.CAMPAIGN,
            "customers/123/campaigns/456",
            business_id,
            connection,
        )
        parent = EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "123", business_id, connection)
        await entities.save(
            AdEntity(
                BusinessId(business_id),
                ref,
                parent,
                f"campaign-{index}",
                AdEntityStatus.ACTIVE,
                PlatformStateHash("a" * 64),
                True,
            )
        )
    for index, account in enumerate(stored_accounts):
        reloaded = await accounts.get_by_ref(account.account_ref)
        assert reloaded is not None and reloaded.credential_ref_id == account.credential_ref_id
        rows = await entities.list_by_account(account.account_ref)
        assert [row.name for row in rows] == [f"campaign-{index}"]
        assert rows[0].entity_ref.connection_id == account.account_ref.connection_id
    assert await accounts.get_by_ref(AccountRef(PlatformCode.GOOGLE, "123")) is None
    assert len(await accounts.list_by_business(BusinessId(business))) == 2
