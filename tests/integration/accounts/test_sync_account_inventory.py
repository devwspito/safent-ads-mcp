"""`SyncAccountInventory` contra Postgres real (migracion 0003/0035): M1
(repaso 0.2.23) -- un `entity_ref` que la plataforma ya no devuelve debe
dejar de ser un objetivo valido para `proposals_entity_exists()` (0036),
algo que `InMemoryAdEntityRepository` no ejercita (no tiene el `JOIN`
contra `platform_accounts` que `list_by_account` sí resuelve en SQL real)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.ports import AccountRef
from safent_ads.accounts.application.sync_account_inventory import SyncAccountInventory
from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.infrastructure.sql_repositories import (
    SqlAccountRepository,
    SqlAdEntityRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityLevel, EntityRef
from tests.conftest import BusinessFactory, OwnerFactory
from tests.integration.packages.conftest import seed_package_scope
from tests.unit.accounts.application.conftest import NOW, FakeAdsPlatformPort

pytestmark = pytest.mark.integration


def _account_ref_from(scope_account_ref: EntityRef) -> AccountRef:
    return AccountRef(
        platform=scope_account_ref.platform,
        external_account_id=scope_account_ref.external_id,
        business_id=scope_account_ref.business_id,
        connection_id=scope_account_ref.connection_id,
    )


async def test_marks_a_vanished_campaign_removed_against_real_ad_entities(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    scope = await seed_package_scope(db_session, owner_id=owner_id, business_id=business_id)
    account_ref = _account_ref_from(scope.account_ref)
    entities = SqlAdEntityRepository(db_session)

    vanished_ref = EntityRef(
        platform=scope.account_ref.platform,
        level=EntityLevel.CAMPAIGN,
        external_id="vanished-on-the-platform",
        business_id=scope.account_ref.business_id,
        connection_id=scope.account_ref.connection_id,
    )
    await entities.save(
        AdEntity(
            business_id=scope.business_id,
            entity_ref=vanished_ref,
            parent_ref=scope.account_ref,
            name="Campana ya borrada en Meta",
            status=AdEntityStatus.PAUSED,
            platform_state_hash=PlatformStateHash.compute({"status": "PAUSED"}),
            is_controllable=True,
        )
    )
    await db_session.flush()

    use_case = SyncAccountInventory(
        FakeAdsPlatformPort(inventory=[]),
        SqlAccountRepository(db_session),
        entities,
        FixedClock(NOW),
    )
    synced_count = await use_case.execute(account_ref)

    assert synced_count == 0
    reloaded = await entities.get_by_ref(vanished_ref)
    assert reloaded is not None
    assert reloaded.status is AdEntityStatus.REMOVED
