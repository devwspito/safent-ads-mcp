"""`SyncAccountInventory`: upsert de `AdEntity` desde el inventario remoto,
calculo de `PlatformStateHash` (T029)."""

from __future__ import annotations

import uuid

import pytest

from safent_ads.accounts.application.errors import AccountNotFoundError
from safent_ads.accounts.application.ports import AccountRef, AdEntitySnapshot
from safent_ads.accounts.application.sync_account_inventory import SyncAccountInventory
from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.budget import Budget, BudgetKind
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money
from safent_ads.accounts.domain.platform_account import ApiTier, PlatformAccount
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.accounts.testing.in_memory_repositories import (
    InMemoryAccountRepository,
    InMemoryAdEntityRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from tests.unit.accounts.application.conftest import NOW, FakeAdsPlatformPort

_ACCOUNT_REF = AccountRef(PlatformCode.GOOGLE, "123-456-7890")


def _account() -> PlatformAccount:
    return PlatformAccount(
        business_id=BusinessId.new(),
        account_ref=_ACCOUNT_REF,
        currency="EUR",
        timezone="Europe/Madrid",
        api_tier=ApiTier.GOOGLE_EXPLORER,
        credential_ref_id=CredentialRefId(uuid.uuid4()),
    )


def _campaign_snapshot(**overrides: object) -> AdEntitySnapshot:
    defaults: dict[str, object] = {
        "entity_ref": EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "1"),
        "parent_ref": EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "123-456-7890"),
        "name": "Campana Otoño",
        "status": AdEntityStatus.ACTIVE,
        "is_controllable": True,
        "learning_state": LearningState.NOT_APPLICABLE,
        "budget": Budget(Money(5000, "EUR"), BudgetKind.DAILY),
        "bid_target": None,
        "shared_budget_ref": None,
        "canonical_state": {"status": "ACTIVE", "budget_minor_units": 5000},
        "fetched_at": NOW,
    }
    defaults.update(overrides)
    return AdEntitySnapshot(**defaults)  # type: ignore[arg-type]


async def test_raises_when_account_unknown() -> None:
    use_case = SyncAccountInventory(
        FakeAdsPlatformPort(),
        InMemoryAccountRepository(),
        InMemoryAdEntityRepository(),
        FixedClock(NOW),
    )

    with pytest.raises(AccountNotFoundError):
        await use_case.execute(_ACCOUNT_REF)


async def test_creates_new_entity_from_snapshot() -> None:
    account = _account()
    entity_repo = InMemoryAdEntityRepository()
    use_case = SyncAccountInventory(
        FakeAdsPlatformPort(inventory=[_campaign_snapshot()]),
        InMemoryAccountRepository([account]),
        entity_repo,
        FixedClock(NOW),
    )

    synced_count = await use_case.execute(_ACCOUNT_REF)

    assert synced_count == 1
    stored = await entity_repo.get_by_ref(EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "1"))
    assert stored is not None
    assert stored.business_id == account.business_id
    assert stored.budget == Budget(Money(5000, "EUR"), BudgetKind.DAILY)


async def test_updates_existing_entity_budget() -> None:
    account = _account()
    entity_repo = InMemoryAdEntityRepository()
    first_sync = SyncAccountInventory(
        FakeAdsPlatformPort(inventory=[_campaign_snapshot()]),
        InMemoryAccountRepository([account]),
        entity_repo,
        FixedClock(NOW),
    )
    await first_sync.execute(_ACCOUNT_REF)

    raised_budget_snapshot = _campaign_snapshot(
        budget=Budget(Money(9000, "EUR"), BudgetKind.DAILY),
        canonical_state={"status": "ACTIVE", "budget_minor_units": 9000},
    )
    second_sync = SyncAccountInventory(
        FakeAdsPlatformPort(inventory=[raised_budget_snapshot]),
        InMemoryAccountRepository([account]),
        entity_repo,
        FixedClock(NOW),
    )
    await second_sync.execute(_ACCOUNT_REF)

    stored = await entity_repo.get_by_ref(EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "1"))
    assert stored is not None
    assert stored.budget == Budget(Money(9000, "EUR"), BudgetKind.DAILY)


async def test_marks_a_locally_known_entity_removed_when_absent_from_the_fresh_inventory() -> None:
    # M1 (repaso 0.2.23): un `entity_ref` que la plataforma ya no devuelve
    # (borrado/dado de baja fuera de este sistema) debe dejar de ser un
    # objetivo valido para `proposals_entity_exists()` (0036) -- el
    # inventario fresco es la UNICA fuente que sabe que desaparecio.
    account = _account()
    vanished_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "vanished")
    entity_repo = InMemoryAdEntityRepository(
        [
            AdEntity(
                business_id=account.business_id,
                entity_ref=vanished_ref,
                parent_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "123-456-7890"),
                name="Campana ya borrada",
                status=AdEntityStatus.ACTIVE,
                platform_state_hash=PlatformStateHash.compute({"status": "ACTIVE"}),
                is_controllable=True,
            )
        ]
    )
    use_case = SyncAccountInventory(
        FakeAdsPlatformPort(inventory=[_campaign_snapshot()]),
        InMemoryAccountRepository([account]),
        entity_repo,
        FixedClock(NOW),
    )

    await use_case.execute(_ACCOUNT_REF)

    kept = await entity_repo.get_by_ref(EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "1"))
    assert kept is not None
    assert kept.status is AdEntityStatus.ACTIVE
    vanished = await entity_repo.get_by_ref(vanished_ref)
    assert vanished is not None
    assert vanished.status is AdEntityStatus.REMOVED


async def test_leaves_an_already_removed_entity_alone() -> None:
    account = _account()
    already_removed_ref = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "gone")
    entity_repo = InMemoryAdEntityRepository(
        [
            AdEntity(
                business_id=account.business_id,
                entity_ref=already_removed_ref,
                parent_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "123-456-7890"),
                name="Campana borrada hace tiempo",
                status=AdEntityStatus.REMOVED,
                platform_state_hash=PlatformStateHash.compute({"status": "REMOVED"}),
                is_controllable=False,
            )
        ]
    )
    use_case = SyncAccountInventory(
        FakeAdsPlatformPort(inventory=[]),
        InMemoryAccountRepository([account]),
        entity_repo,
        FixedClock(NOW),
    )

    await use_case.execute(_ACCOUNT_REF)

    stored = await entity_repo.get_by_ref(already_removed_ref)
    assert stored is not None
    assert stored.status is AdEntityStatus.REMOVED


async def test_records_last_synced_at_on_the_account() -> None:
    account = _account()
    account_repo = InMemoryAccountRepository([account])
    use_case = SyncAccountInventory(
        FakeAdsPlatformPort(inventory=[]),
        account_repo,
        InMemoryAdEntityRepository(),
        FixedClock(NOW),
    )

    await use_case.execute(_ACCOUNT_REF)

    stored_account = await account_repo.get_by_ref(_ACCOUNT_REF)
    assert stored_account is not None
    assert stored_account.last_synced_at == NOW
