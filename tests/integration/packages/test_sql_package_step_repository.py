"""`SqlPackageStepRepository` (T123/AL-4) contra Postgres real: la columna
`confirmed_state_hash` (0049) sobrevive un `upsert`/`get` y solo se admite
junto a `state = 'done'` -- el CHECK de la base (`campaign_package_steps_
confirmed_state_hash_check`) es la ultima frontera, probada aqui a traves
del propio repositorio, no con SQL crudo."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.domain.guardrails import GuardrailEvaluator
from safent_ads.execution.testing.fakes import (
    FakeBrakeStatePort,
    FakeGuardrailSetRepository,
    FakeSpendLedger,
)
from safent_ads.packages.application.approve_campaign_package import (
    ApproveCampaignPackage,
    ApproveCampaignPackageCommand,
)
from safent_ads.packages.application.ports import PackageStepRecord
from safent_ads.packages.domain.campaign_package import CampaignPackage
from safent_ads.packages.domain.identifiers import OfferingId
from safent_ads.packages.infrastructure.sql_package_authorization_repository import (
    SqlPackageAuthorizationRepository,
)
from safent_ads.packages.infrastructure.sql_package_repository import SqlCampaignPackageRepository
from safent_ads.packages.infrastructure.sql_package_step_repository import (
    SqlPackageStepRepository,
)
from safent_ads.packages.infrastructure.sql_publication_repository import (
    SqlPackagePublicationRepository,
)
from safent_ads.proposals.testing.fakes import FakeSignerPort
from safent_ads.shared.clock import FixedClock
from tests.conftest import BusinessFactory, OwnerFactory
from tests.integration.packages.conftest import SeededScope, seed_package_scope
from tests.unit.packages.application.test_approve_campaign_package import _wide_open_guardrails
from tests.unit.packages.domain.conftest import NOW, propose_meta_package

pytestmark = pytest.mark.integration


async def _seed(
    session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> SeededScope:
    owner_id = await owner_factory.create()
    business_id = await business_factory.create()
    return await seed_package_scope(session, owner_id=owner_id, business_id=business_id)


async def _approved_publication(session: AsyncSession, scope: SeededScope) -> str:
    package: CampaignPackage = propose_meta_package(
        business=scope.business_id,
        account=scope.account_ref,
        offering_id=OfferingId(scope.offering_id),
    )
    packages = SqlCampaignPackageRepository(session)
    await packages.add(package)
    await session.flush()
    account_scope = str(package.account_ref)
    approve = ApproveCampaignPackage(
        packages=packages,
        publications=SqlPackagePublicationRepository(session),
        authorizations=SqlPackageAuthorizationRepository(session),
        brakes=FakeBrakeStatePort(),
        guardrail_evaluator=GuardrailEvaluator(),
        guardrail_sets=FakeGuardrailSetRepository(
            {account_scope: _wide_open_guardrails(account_scope)}
        ),
        spend_ledger=FakeSpendLedger(),
        signer=FakeSignerPort(),
        clock=FixedClock(NOW),
    )
    result = await approve.execute(
        ApproveCampaignPackageCommand(
            business_id=package.business_id,
            package_id=package.package_id,
            package_hash=package.package_hash.value,
            approved_by="owner-1",
        )
    )
    await session.flush()
    return result.publication_id


def _pending(publication_id: str) -> PackageStepRecord:
    return PackageStepRecord(
        publication_id=publication_id,
        step_index=1,
        kind="create_campaign",
        local_ref="campaign",
        parent_local_ref=None,
        state="pending",
    )


async def test_confirmed_state_hash_round_trips_through_running_and_done(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scope = await _seed(db_session, owner_factory, business_factory)
    publication_id = await _approved_publication(db_session, scope)
    repo = SqlPackageStepRepository(db_session)
    pending = _pending(publication_id)
    await repo.upsert(pending)
    await repo.upsert(replace(pending, state="running"))

    await repo.upsert(
        replace(
            pending,
            state="done",
            created_entity_ref="meta:campaign:123456",
            confirmed_state_hash="state-after",
        )
    )

    record = await repo.get(publication_id, 1)
    assert record is not None
    assert record.confirmed_state_hash == "state-after"


async def test_confirmed_state_hash_on_a_pending_step_is_rejected_by_the_check(
    db_session: AsyncSession, owner_factory: OwnerFactory, business_factory: BusinessFactory
) -> None:
    scope = await _seed(db_session, owner_factory, business_factory)
    publication_id = await _approved_publication(db_session, scope)
    repo = SqlPackageStepRepository(db_session)

    with pytest.raises(DBAPIError, match="campaign_package_steps_confirmed_state_hash_check"):
        await repo.upsert(replace(_pending(publication_id), confirmed_state_hash="state-after"))
