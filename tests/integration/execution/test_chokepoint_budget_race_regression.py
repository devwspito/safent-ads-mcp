"""Release-blocking regressions: real SQL/composition, only the platform is fake.

Reservations must protect the account across concurrent work and lost responses.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import asyncpg
import pytest
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

from safent_ads.accounts.application.ports import (
    EntityStateSnapshot,
    IdempotencyKey,
    SignedAuthorization,
    WriteIntent,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.composition.container import Container
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind
from safent_ads.execution.infrastructure.sql_spend_ledger import SqlSpendLedger
from safent_ads.proposals.application.submit_approval import SubmitApprovalCommand
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.presentation.panel_read import proposal_detail
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    to_alembic_dsn,
    to_asyncpg_dsn,
    with_database,
)
from tests.contracts.execution.conftest import NOW, GuardrailLimits, seed_guardrails
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.integration.composition.test_write_path_end_to_end import _raise_budget_proposal
from tests.unit.accounts.application.conftest import FakeAdsPlatformPort
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_STATE = {"budget": 70}
_STATE_HASH = PlatformStateHash.compute(_STATE).value


@pytest.fixture
async def isolated_database_url(
    postgres_container: PostgresContainer,
) -> AsyncIterator[str]:
    # These tests intentionally COMMIT queued/UNKNOWN work across sessions and
    # restarts. Sharing the normal rollback database would let a later global
    # worker claim our leftover execution instead of that test's own proposal.
    base = postgres_container.get_connection_url()
    name = f"ads_budget_race_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(to_asyncpg_dsn(base))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    dsn = with_database(to_alembic_dsn(base), name)
    try:
        alembic_upgrade(dsn)
        yield dsn
    finally:
        admin = await asyncpg.connect(to_asyncpg_dsn(base))
        try:
            await admin.execute(f'DROP DATABASE "{name}"')
        finally:
            await admin.close()


class _ControlledPlatform(FakeAdsPlatformPort):
    def __init__(self, *, hold_first: bool = False, fail_first: bool = False) -> None:
        super().__init__()
        self.hold_first = hold_first
        self.fail_first = fail_first
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.mutations: list[EntityRef] = []
        self.receipts: dict[str, WriteOutcome] = {}
        self.receipt_reads = 0

    async def execute_write(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,  # noqa: ARG002 - port contract; verified by chokepoint
        idempotency_key: IdempotencyKey,  # noqa: ARG002 - two distinct approved proposals
    ) -> WriteOutcome:
        self.mutations.append(intent.entity_ref)
        receipt = WriteOutcome(
            outcome="SUCCEEDED",
            applied_value=intent.valor_propuesto,
            state_hash_after=_STATE_HASH,
            error_code=None,
            platform_request_id="fake-only",
        )
        self.receipts[str(idempotency_key)] = receipt
        if len(self.mutations) == 1:
            self.first_started.set()
            if self.hold_first:
                await self.release_first.wait()
            if self.fail_first:
                # The vendor applied the change; its acknowledgement was lost.
                raise TimeoutError("response lost after remote mutation")
        return receipt

    async def read_write_receipt(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome | None:
        del intent, authorization
        self.receipt_reads += 1
        return self.receipts.get(str(idempotency_key))


async def _seed_two_approved(
    container: Container,
    platform: _ControlledPlatform,
    *,
    same_account: bool,
    other_connection: bool = False,
) -> tuple[ProposalId, ProposalId]:
    refs = (campaign_ref(uuid.uuid4().hex), campaign_ref(uuid.uuid4().hex))
    async with container.session_factory() as session:
        business = await seed_entity(session, refs[0])
        if other_connection:
            owner, connection, account_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            await session.execute(
                text("""INSERT INTO owners(id,email,password_hash)
                VALUES(:id,:email,'test-not-login')"""),
                {"id": owner, "email": f"{owner}@test.invalid"},
            )
            await session.execute(
                text("""INSERT INTO platform_connections(id,business_id,owner_id,platform)
                SELECT :connection,business_id,:owner,platform
                  FROM ad_entities WHERE entity_ref=:first"""),
                {"connection": connection, "owner": owner, "first": str(refs[0])},
            )
            await session.execute(
                text("""INSERT INTO platform_accounts
                (id,business_id,platform,external_account_id,currency,timezone,api_tier,
                 credential_ref_id,status,connection_id)
                SELECT :id,a.business_id,a.platform,a.external_account_id,a.currency,
                       a.timezone,a.api_tier,
                       a.credential_ref_id,a.status,:connection
                  FROM platform_accounts a JOIN ad_entities e ON e.platform_account_id=a.id
                 WHERE e.entity_ref=:first"""),
                {"id": account_id, "connection": connection, "first": str(refs[0])},
            )
            refs = (refs[0], replace(refs[1], business_id=business, connection_id=connection))
            await session.execute(
                text("""INSERT INTO ad_entities
                (business_id,platform_account_id,platform,level,external_id,name,status,
                 platform_state_hash,connection_id)
                SELECT business_id,:account,platform,level,:external,
                       'Second connection campaign',status,
                       platform_state_hash,:connection FROM ad_entities WHERE entity_ref=:first"""),
                {
                    "account": account_id,
                    "external": refs[1].external_id,
                    "connection": connection,
                    "first": str(refs[0]),
                },
            )
            businesses = (business, business)
        elif same_account:
            await session.execute(
                text("""
                INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                         external_id, name, status, platform_state_hash)
                SELECT business_id, platform_account_id, platform, level,
                       :external_id, 'Second campaign', status, platform_state_hash
                  FROM ad_entities WHERE entity_ref = :first
            """),
                {"external_id": refs[1].external_id, "first": str(refs[0])},
            )
            businesses = (business, business)
        else:
            businesses = (business, await seed_entity(session, refs[1]))
        for ref in refs if not same_account else refs[:1]:
            await seed_guardrails(
                session,
                scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(ref)),
                limits=GuardrailLimits(daily_cap="20", max_step_pct=1.0),
                level="business",
            )
        ids = []
        for ref, business_id in zip(refs, businesses, strict=True):
            platform.entity_states[ref] = EntityStateSnapshot(
                entity_ref=ref,
                status=AdEntityStatus.ACTIVE,
                is_controllable=True,
                canonical_state=_STATE,
                fetched_at=NOW,
            )
            cases = container.build_execution_use_cases(session)
            proposal = _raise_budget_proposal(
                BusinessId(business_id), ref, expected_state_hash=_STATE_HASH
            )
            await cases.proposals.save(proposal)
            await cases.submit_approval.execute(
                SubmitApprovalCommand(
                    proposal_id=proposal.proposal_id,
                    diff_hash=proposal.diff.diff_hash,
                    approved_by="test-owner",
                    channel=AuthorizationChannel.PANEL,
                )
            )
            ids.append(proposal.proposal_id)
        await session.commit()
    container.clock = FixedClock(NOW + timedelta(minutes=1))
    return ids[0], ids[1]


async def _run(container: Container, proposal_id: ProposalId) -> ExecutionStatus | None:
    async with container.session_factory() as session:
        result = await container.build_execution_use_cases(session).chokepoint.run_once(
            proposal_id=proposal_id
        )
        await session.commit()
        return result


@pytest.mark.parametrize(
    "same_account,other_connection",
    [
        pytest.param(True, False, id="same-account-reserved"),
        pytest.param(True, True, id="same-physical-account-other-connection"),
        pytest.param(False, False, id="different-accounts-independent"),
    ],
)
async def test_inflight_change_cannot_be_overspent_by_another_campaign(
    isolated_database_url: str,
    same_account: bool,
    other_connection: bool,
) -> None:
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    platform = _ControlledPlatform(hold_first=True)
    container.ads_platform_port = platform
    try:
        first_id, second_id = await _seed_two_approved(
            container,
            platform,
            same_account=same_account,
            other_connection=other_connection,
        )
        first = asyncio.create_task(_run(container, first_id))
        try:
            await asyncio.wait_for(platform.first_started.wait(), timeout=3)
            second_result = await asyncio.wait_for(_run(container, second_id), timeout=3)
        finally:
            platform.release_first.set()
            first_result = await asyncio.wait_for(first, timeout=3)
        assert first_result is ExecutionStatus.EXECUTED
        if same_account:
            assert len(platform.mutations) <= 1, (
                f"daily cap 20 EUR overshot by two +20 EUR changes; outcomes "
                f"{first_result}, {second_result}"
            )
        else:
            assert second_result is ExecutionStatus.EXECUTED
            assert len(platform.mutations) == 2
    finally:
        await container.aclose()


@pytest.mark.parametrize("receipt_available", [True, False])
async def test_restart_reconciles_read_only_even_after_authorization_expires(
    isolated_database_url: str,
    receipt_available: bool,
) -> None:
    settings = build_api_settings(database_url=isolated_database_url)
    original = Container.build(settings)
    original.clock = FixedClock(NOW)
    platform = _ControlledPlatform(fail_first=True)
    original.ads_platform_port = platform
    first_id, _ = await _seed_two_approved(original, platform, same_account=True)
    assert await _run(original, first_id) is ExecutionStatus.UNKNOWN
    await original.aclose()
    if not receipt_available:
        platform.receipts.clear()
    restarted = Container.build(settings)
    restarted.clock = FixedClock(NOW + timedelta(days=40))
    restarted.ads_platform_port = platform
    try:
        result = await _run(restarted, first_id)
        assert result is (
            ExecutionStatus.EXECUTED if receipt_available else ExecutionStatus.UNKNOWN
        )
        assert len(platform.mutations) == 1
        assert platform.receipt_reads == 1
        async with restarted.session_factory() as session:
            reservation = (
                await session.execute(
                    text(
                        "SELECT state FROM execution_reservations r "
                        "JOIN executions e ON e.id = r.execution_id "
                        "WHERE e.proposal_id = :proposal"
                    ),
                    {"proposal": str(first_id)},
                )
            ).scalar_one()
            assert reservation == ("SETTLED" if receipt_available else "ACTIVE")
            ledger_count = (
                await session.execute(
                    text("SELECT count(*) FROM spend_ledger WHERE proposal_id = :proposal"),
                    {"proposal": str(first_id)},
                )
            ).scalar_one()
            assert ledger_count == int(receipt_available)

            row = (
                await session.execute(
                    text("SELECT id, business_id FROM executions WHERE proposal_id = :proposal"),
                    {"proposal": str(first_id)},
                )
            ).one()
            detail = await proposal_detail(session, str(row.business_id), str(first_id))
            assert detail["execution_id"] == str(row.id)
            if not receipt_available:
                proposal = await restarted.build_execution_use_cases(session).proposals.get(
                    first_id
                )
                assert proposal is not None
                scope = GuardrailScope(kind=ScopeKind.ENTITY, ref=str(proposal.diff.entity_ref))
                cases = restarted.build_execution_use_cases(session)
                ledger = await SqlSpendLedger(
                    session, restarted.clock, cases.execution_queue
                ).snapshot(scope, proposal.diff.entity_ref)
                assert ledger.reserved_increase.amount == 20
                assert ledger.changes_count_today_for_entity == 1
        # Even repeated reconciliation after lease expiry never remutates.
        restarted.clock = FixedClock(NOW + timedelta(days=41))
        again = await _run(restarted, first_id)
        assert again is (None if receipt_available else ExecutionStatus.UNKNOWN)
        assert len(platform.mutations) == 1
    finally:
        await restarted.aclose()


@pytest.mark.parametrize("other_connection", [False, True])
async def test_timeout_after_mutation_does_not_release_budget_for_another_change(
    isolated_database_url: str,
    other_connection: bool,
) -> None:
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    platform = _ControlledPlatform(fail_first=True)
    container.ads_platform_port = platform
    try:
        first_id, second_id = await _seed_two_approved(
            container, platform, same_account=True, other_connection=other_connection
        )
        first = await _run(container, first_id)
        assert first is ExecutionStatus.UNKNOWN
        with pytest.raises(Exception, match="Cannot downgrade with unresolved executions"):
            await asyncio.to_thread(
                alembic_downgrade, isolated_database_url, "0033_crm_bridge_health"
            )
        second = await _run(container, second_id)
        assert len(platform.mutations) <= 1, (
            f"first change really applied but response lost; a second change spent its "
            f"unreserved budget; outcomes {first}, {second}"
        )
    finally:
        await container.aclose()
