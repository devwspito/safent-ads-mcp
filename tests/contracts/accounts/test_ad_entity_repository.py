"""Contrato de `AdEntityRepository`: identico para el doble en memoria y para
`SqlAdEntityRepository`."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.shared.ids import EntityLevel, PlatformCode
from tests.contracts.accounts.conftest import (
    DAILY_BUDGET,
    RepositoryFixture,
    account_parent_ref,
    account_ref,
    build_account,
    build_entity,
    new_business_id,
)

DRIFT_AT = datetime(2026, 9, 9, 13, tzinfo=UTC)


async def _account(repositories: RepositoryFixture, suffix: str = "1"):
    business_id = new_business_id()
    await repositories.given_business(business_id, PlatformCode.META)
    ref = account_ref(suffix=suffix)
    await repositories.accounts.save(build_account(business_id, ref=ref))
    return business_id, ref


async def test_saved_entity_is_read_back_whole(repositories: RepositoryFixture) -> None:
    business_id, ref = await _account(repositories)
    campaign = build_entity(
        business_id,
        parent=account_parent_ref(ref),
        budget=DAILY_BUDGET,
        bid_target=Money(2500, "EUR"),
        learning_state=LearningState.LEARNING,
        is_controllable=False,
        state_hash="b" * 64,
    )

    await repositories.entities.save(campaign)
    stored = await repositories.entities.get_by_ref(campaign.entity_ref)

    assert stored is not None
    assert stored.business_id == business_id
    assert stored.entity_ref == campaign.entity_ref
    assert stored.parent_ref == account_parent_ref(ref)
    assert stored.name == campaign.name
    assert stored.status == AdEntityStatus.ACTIVE
    assert stored.budget == DAILY_BUDGET
    assert stored.bid_target == Money(2500, "EUR")
    assert stored.learning_state == LearningState.LEARNING
    assert stored.is_controllable is False
    assert stored.platform_state_hash == PlatformStateHash("b" * 64)


async def test_unknown_entity_is_none(repositories: RepositoryFixture) -> None:
    business_id, ref = await _account(repositories)
    missing = build_entity(business_id, parent=account_parent_ref(ref), external_id="ausente")

    assert await repositories.entities.get_by_ref(missing.entity_ref) is None


async def test_whole_hierarchy_round_trips(repositories: RepositoryFixture) -> None:
    business_id, ref = await _account(repositories)
    campaign = build_entity(business_id, parent=account_parent_ref(ref))
    ad_set = build_entity(
        business_id, level=EntityLevel.AD_SET, external_id="s-1", parent=campaign.entity_ref
    )
    ad = build_entity(
        business_id, level=EntityLevel.AD, external_id="a-1", parent=ad_set.entity_ref
    )
    creative = build_entity(
        business_id, level=EntityLevel.CREATIVE, external_id="cr-1", parent=ad.entity_ref
    )

    await repositories.entities.save_many([creative, ad, ad_set, campaign])
    stored = await repositories.entities.list_by_account(ref)

    by_ref = {entity.entity_ref: entity for entity in stored}
    assert len(stored) == 4
    assert by_ref[ad_set.entity_ref].parent_ref == campaign.entity_ref
    assert by_ref[ad.entity_ref].parent_ref == ad_set.entity_ref
    assert by_ref[creative.entity_ref].parent_ref == ad.entity_ref


async def test_resync_updates_instead_of_duplicating(repositories: RepositoryFixture) -> None:
    business_id, ref = await _account(repositories)
    campaign = build_entity(business_id, parent=account_parent_ref(ref), budget=DAILY_BUDGET)
    await repositories.entities.save(campaign)

    campaign.refresh_from_platform(
        name="Búsqueda Marca (renombrada)",
        status=AdEntityStatus.PAUSED,
        is_controllable=True,
        learning_state=LearningState.LEARNING,
        budget=DAILY_BUDGET,
        bid_target=None,
        shared_budget_ref=None,
        new_hash=PlatformStateHash("c" * 64),
        occurred_at=DRIFT_AT,
    )
    await repositories.entities.save(campaign)

    stored = await repositories.entities.list_by_account(ref)
    assert len(stored) == 1
    assert stored[0].name == "Búsqueda Marca (renombrada)"
    assert stored[0].status == AdEntityStatus.PAUSED
    assert stored[0].platform_state_hash == PlatformStateHash("c" * 64)


async def test_drift_marking_survives_the_round_trip(repositories: RepositoryFixture) -> None:
    business_id, ref = await _account(repositories)
    campaign = build_entity(business_id, parent=account_parent_ref(ref))
    await repositories.entities.save(campaign)

    stored = await repositories.entities.get_by_ref(campaign.entity_ref)
    assert stored is not None
    stored.mark_drifted(PlatformStateHash("d" * 64), occurred_at=DRIFT_AT)
    await repositories.entities.save(stored)

    reloaded = await repositories.entities.get_by_ref(campaign.entity_ref)
    assert reloaded is not None
    assert reloaded.status == AdEntityStatus.DRIFTED


async def test_list_by_account_ignores_other_accounts(repositories: RepositoryFixture) -> None:
    business_id, ref = await _account(repositories)
    other_ref = account_ref(suffix="2")
    await repositories.accounts.save(build_account(business_id, ref=other_ref))
    mine = build_entity(business_id, parent=account_parent_ref(ref), external_id="mia")
    theirs = build_entity(business_id, parent=account_parent_ref(other_ref), external_id="ajena")
    await repositories.entities.save_many([mine, theirs])

    stored = await repositories.entities.list_by_account(ref)

    assert [entity.entity_ref for entity in stored] == [mine.entity_ref]
