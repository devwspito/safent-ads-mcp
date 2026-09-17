"""PostgreSQL integrity, collisions, durable UNKNOWN context and safe downgrade."""

import asyncio
import base64
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.composition.signing import build_approval_key_pair
from safent_ads.execution.domain.execution_attempt import ExecutionAttempt
from safent_ads.execution.infrastructure.sql_execution_queue import SqlExecutionQueue
from safent_ads.execution.infrastructure.sql_execution_reservations import SqlExecutionReservations
from safent_ads.proposals.domain.authorization import sign_authorization
from safent_ads.proposals.domain.proposal import ProposalInvariantError, ProposedDiff
from safent_ads.proposals.infrastructure.sql_authorization_repository import (
    SqlAuthorizationRepository,
)
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId
from safent_ads.shared.managed_ads import ManagedAdsBinding, binding_to_json
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.integration.execution.test_physical_controls_sql import clone_connection
from tests.integration.proposals.test_physical_equivalence import _NOW, command, use_case
from tests.unit.broker.test_managed_signed_context import signed

pytestmark = pytest.mark.integration


async def scoped_pair(session):
    original = campaign_ref(uuid4().hex, "google")
    business = BusinessId(await seed_entity(session, original))
    remote = str(uuid4().int)[:20]
    await session.execute(
        text(
            "UPDATE platform_accounts SET external_account_id=:remote WHERE business_id=:business"
        ),
        {"remote": remote, "business": business.value},
    )
    a, raw_a = await clone_connection(session, original)
    b, raw_b = await clone_connection(session, original)
    identity = dict(
        grant_id=uuid4(),
        revision=1,
        org_id=uuid4(),
        user_id=uuid4(),
        employee_id=uuid4(),
        instance_id=uuid4(),
        resource_revision=1,
    )
    return (
        business,
        a,
        b,
        ManagedAdsBinding(account=AccountRef.parse(raw_a), **identity),
        ManagedAdsBinding(account=AccountRef.parse(raw_b), **{**identity, "grant_id": uuid4()}),
    )


def managed_command(business, ref, binding):
    cmd = command(business, ref)
    return replace(
        cmd,
        diff=ProposedDiff.build(
            ref, cmd.diff.parameter, cmd.diff.before, cmd.diff.after, managed_binding=binding
        ),
    )


async def test_scope_collision_never_returns_or_reparents_other_principal_proposal(db_session):
    business, a, b, bind_a, bind_b = await scoped_pair(db_session)
    service = use_case(db_session)
    cmd = managed_command(business, a, bind_a)
    first = await service.execute(cmd)
    assert (await service.execute(cmd)).proposal_id == first.proposal_id
    for other in (
        managed_command(business, b, bind_b),
        managed_command(business, a, replace(bind_a, user_id=uuid4())),
        command(business, a),
    ):
        with pytest.raises(ProposalInvariantError, match="physical_action_conflict"):
            await service.execute(other)
    stored = await SqlProposalRepository(db_session).get(first.proposal_id)
    assert stored.diff.managed_binding == bind_a
    assert stored.diff.diff_hash == first.diff_hash


async def test_sql_rejects_binding_replacement_wrong_account_and_signature_context(db_session):
    business, a, _, bind_a, _ = await scoped_pair(db_session)
    first = await use_case(db_session).execute(managed_command(business, a, bind_a))
    for binding in (None, replace(bind_a, revision=2)):
        with pytest.raises(DBAPIError, match="managed_binding_immutable"):
            async with db_session.begin_nested():
                await db_session.execute(
                    text("UPDATE proposals SET managed_binding=CAST(:b AS JSONB) WHERE id=:id"),
                    {"b": binding_to_json(binding), "id": str(first.proposal_id)},
                )
    # Domain scope checks connection; only SQL can prove the entity's real parent account.
    wrong = replace(bind_a, account=replace(bind_a.account, external_account_id="999"))
    with pytest.raises(DBAPIError, match="managed_binding_account_mismatch"):
        async with db_session.begin_nested():
            cmd = managed_command(business, a, wrong)
            await use_case(db_session).execute(
                replace(
                    cmd,
                    diff=ProposedDiff.build(
                        a, "other_budget", cmd.diff.before, cmd.diff.after, managed_binding=wrong
                    ),
                )
            )
    _, authorization, _ = signed(managed=False)
    authorization = replace(authorization, proposal_id=first.proposal_id)
    with pytest.raises(DBAPIError, match="managed_binding_approval_mismatch"):
        async with db_session.begin_nested():
            await SqlAuthorizationRepository(db_session).save(authorization)


async def test_restart_preserves_signed_reservation_unknown(isolated_database_url):
    engine = create_async_engine(isolated_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            business, a, _, bind_a, _ = await scoped_pair(session)
            first = await use_case(session).execute(managed_command(business, a, bind_a))
            proposal = await SqlProposalRepository(session).get(first.proposal_id)
            _, base, _ = signed()
            # Re-sign fixture using the real signing implementation; no route can
            # issue this until human Enterprise admission is added.
            authorization = sign_authorization(
                authorization_id=base.authorization_id,
                proposal_id=first.proposal_id,
                kind=base.kind,
                proposal_classification=proposal.classification,
                diff_hash=proposal.diff.diff_hash,
                guardrail_verdict_hash=base.guardrail_verdict_hash,
                issued_by=base.issued_by,
                channel=base.channel,
                decided_at=_NOW,
                expires_at=_NOW + timedelta(minutes=15),
                signer=build_approval_key_pair(base64.b64encode(b"m" * 32).decode()).signer,
                managed_binding=bind_a,
            )
            await SqlAuthorizationRepository(session).save(authorization)
            attempt = ExecutionAttempt.claim(
                business, first.proposal_id, authorization.authorization_id, first.diff_hash, _NOW
            )
            attempt.previous_value = proposal.diff.before
            attempt.platform_state_hash_before = "a" * 64
            attempt.mark_unknown("test-lost-ack")
            await SqlExecutionQueue(session, FixedClock(_NOW)).save(attempt)
            reservations = SqlExecutionReservations(session, FixedClock(_NOW))
            await reservations.reserve(attempt, proposal.diff)
            await session.commit()
        await engine.dispose()
        async with factory() as restarted:
            loaded = await SqlExecutionReservations(
                restarted, FixedClock(_NOW + timedelta(days=2))
            ).get(attempt)
            auth = await SqlAuthorizationRepository(restarted).get(authorization.authorization_id)
            assert loaded == proposal.diff
            assert auth == authorization
            assert auth.signing_payload() == authorization.signing_payload()
            assert (
                await restarted.execute(
                    text("SELECT state FROM execution_reservations WHERE execution_id=:id"),
                    {"id": attempt.execution_id.value},
                )
            ).scalar_one() == "ACTIVE"
            with pytest.raises(DBAPIError, match="managed_binding_immutable"):
                async with restarted.begin_nested():
                    await restarted.execute(
                        text(
                            "UPDATE execution_reservations SET managed_binding=NULL "
                            "WHERE execution_id=:id"
                        ),
                        {"id": attempt.execution_id.value},
                    )
    finally:
        await engine.dispose()


async def test_concurrent_oauth_grants_have_one_physical_slot(isolated_database_url):
    engine = create_async_engine(isolated_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            business, a, b, bind_a, bind_b = await scoped_pair(session)
            await session.commit()

        async def propose(ref, binding):
            async with factory() as session:
                result = await use_case(session).execute(managed_command(business, ref, binding))
                await session.commit()
                return result

        results = await asyncio.gather(
            propose(a, bind_a), propose(b, bind_b), return_exceptions=True
        )
        assert sum(isinstance(result, ProposalInvariantError) for result in results) == 1
        assert sum(not isinstance(result, Exception) for result in results) == 1
    finally:
        await engine.dispose()
