"""Physical proposal identity never permits OAuth switching or reauthorization."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from safent_ads.proposals.application.propose_action import ProposeAction, ProposeActionCommand
from safent_ads.proposals.domain.cause import Cause
from safent_ads.proposals.domain.classification import ClassificationPolicy, ProposalKind
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import ExpiryPolicy, Urgency
from safent_ads.proposals.domain.proposal import ProposalInvariantError, ProposedDiff
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.integration.execution.test_physical_controls_sql import (
    child_with_same_remote_id,
    clone_connection,
)
from tests.integration.opportunities.test_sql_repositories import _NOW

pytestmark = pytest.mark.integration


def use_case(session: AsyncSession) -> ProposeAction:
    return ProposeAction(
        proposals=SqlProposalRepository(session),
        classification_policy=ClassificationPolicy(critical_impact_threshold=Money.of("2000")),
        expiry_policy=ExpiryPolicy(),
        clock=FixedClock(_NOW),
    )


def command(business: BusinessId, ref: EntityRef, *, after: str = "90") -> ProposeActionCommand:
    return ProposeActionCommand(
        business_id=business,
        diff=ProposedDiff.build(
            entity_ref=ref, parameter="daily_budget", before=Money.of("70"), after=Money.of(after)
        ),
        kind=ProposalKind.BUDGET_INCREASE,
        cause=Cause(text="Physical signal"),
        cause_type="physical_signal",
        evidence=(),
        estimated_impact=Money.of("20"),
        urgency=Urgency.RECOMMENDED,
    )


async def test_physical_equivalence_is_not_parameter_or_scope_confusion(
    db_session: AsyncSession,
) -> None:
    original = campaign_ref("physical-proposal", "google")
    business = BusinessId(await seed_entity(db_session, original))
    a, _ = await clone_connection(db_session, original)
    b, _ = await clone_connection(db_session, original)
    outside, _ = await clone_connection(db_session, original, other_business=True)
    account, _ = await clone_connection(db_session, original, other_account=True)
    child = await child_with_same_remote_id(db_session, b)
    service = use_case(db_session)
    first = await service.execute(command(business, a))
    same = await service.execute(command(business, b))
    assert same.proposal_id == first.proposal_id and same.diff_hash == first.diff_hash
    with pytest.raises(ProposalInvariantError, match="physical_action_conflict"):
        await service.execute(command(business, b, after="100"))
    with pytest.raises(ProposalInvariantError, match="business_mismatch"):
        await service.execute(command(BusinessId.new(), a))
    separate = [await service.execute(command(business, ref)) for ref in (account, child)]
    separate.append(await service.execute(command(BusinessId(outside.business_id), outside)))
    pause = replace(
        command(business, b),
        diff=ProposedDiff.build(entity_ref=b, parameter="status", before="ACTIVE", after="PAUSED"),
        kind=ProposalKind.PAUSE,
    )
    separate.append(await service.execute(pause))
    assert len({item.proposal_id for item in [first, *separate]}) == 5


@pytest.mark.parametrize("unknown", [False, True])
async def test_approved_or_unknown_terminal_history_stays_bound(
    db_session: AsyncSession,
    unknown: bool,
) -> None:
    original = campaign_ref(f"physical-unknown-{unknown}", "google")
    business = BusinessId(await seed_entity(db_session, original))
    a, account_a = await clone_connection(db_session, original)
    b, _ = await clone_connection(db_session, original)
    first = await use_case(db_session).execute(command(business, a))
    params = {"id": str(first.proposal_id)}
    await db_session.execute(text("UPDATE proposals SET state='approved' WHERE id=:id"), params)
    approval = (
        await db_session.execute(
            text("""INSERT INTO approvals
        (proposal_id,kind,decision,diff_hash,guardrail_verdict_hash,issued_by,channel,
         signature,decided_at,expires_at)
        SELECT id,'human_approval','approved',diff_hash,repeat('a',64),'test-human',
               'panel','test-signature-not-live',now(),now()+interval '1 hour'
        FROM proposals WHERE id=:id RETURNING id"""),
            params,
        )
    ).scalar_one()
    if unknown:
        await db_session.execute(
            text("UPDATE proposals SET state='executing' WHERE id=:id"), params
        )
        await db_session.execute(text("UPDATE proposals SET state='failed' WHERE id=:id"), params)
        await db_session.execute(
            text("""INSERT INTO executions
            (proposal_id,authorization_id,business_id,entity_ref,idempotency_key,
             previous_value,platform_state_hash_before,outcome)
            SELECT id,:approval,business_id,entity_ref,'exec-'||id::text||'-aaaaaaaaaaaa',
                   current_value,repeat('a',64),'UNKNOWN' FROM proposals WHERE id=:id"""),
            {**params, "approval": approval},
        )
    before = (
        await db_session.execute(
            text("SELECT row_to_json(p)::text FROM proposals p WHERE id=:id"), params
        )
    ).scalar_one()
    await db_session.execute(
        text("UPDATE platform_accounts SET status='SUSPENDED' WHERE account_ref=:ref"),
        {"ref": account_a},
    )
    result = await use_case(db_session).execute(command(business, b))
    assert result.proposal_id == first.proposal_id
    assert (
        await db_session.execute(
            text("SELECT row_to_json(p)::text FROM proposals p WHERE id=:id"), params
        )
    ).scalar_one() == before
    assert (
        await db_session.execute(
            text("SELECT count(*) FROM approvals WHERE proposal_id=:id"), params
        )
    ).scalar_one() == 1


async def test_concurrent_connections_and_rollback_reclaim_same_physical_slot(
    isolated_database_url: str,
) -> None:
    engine = create_async_engine(isolated_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            original = campaign_ref(f"physical-action-race-{uuid4().hex}", "google")
            business = BusinessId(await seed_entity(session, original))
            a, _ = await clone_connection(session, original)
            b, _ = await clone_connection(session, original)
            other, _ = await clone_connection(session, original, other_account=True)
            await session.commit()

        async def create(ref: EntityRef):  # noqa: ANN202 - local task result
            async with factory() as session:
                result = await use_case(session).execute(command(business, ref))
                await session.commit()
                return result

        async with factory() as interrupted:
            abandoned = await use_case(interrupted).execute(command(business, a))
            # A distinct physical account proceeds while A's transaction is held.
            independent = await asyncio.wait_for(create(other), timeout=3)
            assert independent.proposal_id != abandoned.proposal_id
            waiting = asyncio.create_task(create(b))
            await asyncio.sleep(0.03)
            assert not waiting.done()
            await interrupted.rollback()  # same release semantics as a lost DB connection
            recovered = await asyncio.wait_for(waiting, timeout=3)
        raced = await asyncio.gather(create(a), create(b))
        assert {x.proposal_id for x in raced} == {recovered.proposal_id}
        assert recovered.proposal_id != abandoned.proposal_id
    finally:
        await engine.dispose()
