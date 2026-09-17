"""`SqlCampaignPackageRepository` (T027) contra Postgres real: recalcula
`package_hash` al leer, deduplica por `(business_id, account_ref,
offering_id)` y sobrevive el redondeo por `platform_accounts.account_ref`
generado."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.packages.application.errors import DuplicateOpenPackageError
from safent_ads.packages.domain.campaign_package import PackageState
from safent_ads.packages.domain.identifiers import OfferingId, PackageId
from safent_ads.packages.infrastructure.sql_package_repository import (
    SqlCampaignPackageRepository,
)
from tests.conftest import BusinessFactory, OwnerFactory
from tests.integration.packages.conftest import SeededScope, seed_package_scope
from tests.unit.packages.domain.conftest import propose_meta_package

pytestmark = pytest.mark.integration


async def _seed(
    session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> SeededScope:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    return await seed_package_scope(session, owner_id=owner_id, business_id=business_id)


def _package(scope: SeededScope):
    return propose_meta_package(
        business=scope.business_id,
        account=scope.account_ref,
        offering_id=OfferingId(scope.offering_id),
    )


async def test_add_then_get_recomputes_the_hash_from_the_live_plan(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scope = await _seed(db_session, owner_factory, business_factory)
    package = _package(scope)
    repo = SqlCampaignPackageRepository(db_session)

    await repo.add(package)
    fetched = await repo.get(package.package_id, business_id=scope.business_id)

    assert fetched is not None
    assert fetched.package_hash == package.package_hash
    assert fetched.campaign.name == package.campaign.name
    assert fetched.ad_sets[0].ads[0].creative == package.ad_sets[0].ads[0].creative
    assert fetched.state is PackageState.PROPOSED


async def test_get_returns_none_for_another_business(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scope = await _seed(db_session, owner_factory, business_factory)
    package = _package(scope)
    repo = SqlCampaignPackageRepository(db_session)
    await repo.add(package)

    other_owner = await owner_factory.create()
    other_business = await business_factory.create()
    other_scope = await seed_package_scope(
        db_session, owner_id=other_owner, business_id=other_business
    )

    assert await repo.get(package.package_id, business_id=other_scope.business_id) is None


async def test_find_open_duplicate_matches_business_account_and_offering(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scope = await _seed(db_session, owner_factory, business_factory)
    package = _package(scope)
    repo = SqlCampaignPackageRepository(db_session)
    await repo.add(package)

    duplicate = await repo.find_open_duplicate(
        business_id=scope.business_id,
        account_ref=scope.account_ref,
        offering_id=OfferingId(scope.offering_id),
    )

    assert duplicate == package.package_id
    assert (
        await repo.find_open_duplicate(
            business_id=scope.business_id,
            account_ref=scope.account_ref,
            offering_id=OfferingId(str(uuid.uuid4())),
        )
        is None
    )


async def test_the_database_unique_index_backs_up_the_proactive_dedup_check(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scope = await _seed(db_session, owner_factory, business_factory)
    repo = SqlCampaignPackageRepository(db_session)
    await repo.add(_package(scope))
    await db_session.flush()

    with pytest.raises(DuplicateOpenPackageError):
        await repo.add(_package(scope))
        await db_session.flush()


async def test_save_persists_a_rejected_state_transition(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scope = await _seed(db_session, owner_factory, business_factory)
    package = _package(scope)
    repo = SqlCampaignPackageRepository(db_session)
    await repo.add(package)
    await db_session.flush()

    package.reject(package.created_at, "no aplica")
    await repo.save(package)
    await db_session.flush()

    fetched = await repo.get(package.package_id, business_id=scope.business_id)
    assert fetched is not None
    assert fetched.state is PackageState.REJECTED


async def test_get_of_unknown_package_id_is_none(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scope = await _seed(db_session, owner_factory, business_factory)
    repo = SqlCampaignPackageRepository(db_session)

    assert await repo.get(PackageId.new(), business_id=scope.business_id) is None
