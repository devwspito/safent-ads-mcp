"""Real approval/queue/reservations/ledger; provider port only is fake."""

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from safent_ads.composition.container import Container
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.proposals.application.submit_approval import SubmitApprovalCommand
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityRef
from tests.conftest import OwnerFactory
from tests.contracts.execution.conftest import NOW
from tests.integration.composition.test_write_path_end_to_end import (
    _APPROVAL_SEED_B64,
    _raise_budget_proposal,
    _running_broker,
)
from tests.integration.execution.test_chokepoint_budget_race_regression import (
    _ControlledPlatform,
    _run,
)
from tests.integration.execution.test_chokepoint_budget_race_regression import (
    isolated_database_url as isolated_database_url,  # noqa: PLC0414 - fixture re-export
)
from tests.integration.opportunities.test_sql_repositories import _seed_business_with_account
from tests.unit.composition.factories import build_api_settings
from tests.unit.execution.test_campaign_creation_budget import creation_payload

pytestmark = pytest.mark.integration


async def seed_creations(container: Container, *, same_account: bool = True):
    ids = []
    async with container.session_factory() as session:
        business = await _seed_business_with_account(session, suffix=uuid4().hex)
        owner = await OwnerFactory(session).create()
        for index in range(2):
            connection = uuid4()
            await session.execute(
                text("""INSERT INTO platform_connections(id,business_id,owner_id,platform)
                VALUES(:id,:business,:owner,'google')"""),
                {"id": connection, "business": business.value, "owner": owner},
            )
            account_ref = (
                await session.execute(
                    text("""INSERT INTO platform_accounts
                (business_id,platform,connection_id,external_account_id,currency,timezone,api_tier,status)
                VALUES(:business,'google',:connection,:remote,'EUR','Europe/Madrid','google_standard','ACTIVE')
                RETURNING account_ref"""),
                    {
                        "business": business.value,
                        "connection": connection,
                        "remote": "123" if same_account else str(index + 123),
                    },
                )
            ).scalar_one()
            ref = EntityRef.parse(account_ref)
            proposal = _raise_budget_proposal(business, ref)
            proposal.diff = ProposedDiff.build(
                entity_ref=ref,
                parameter=f"new_campaign:{index}",
                before=None,
                after=creation_payload(),
            )
            proposal.classification = Classification.IMPORTANT
            cases = container.build_execution_use_cases(session)
            await cases.proposals.save(proposal)
            ids.append(proposal.proposal_id)
        await session.execute(
            text("""INSERT INTO guardrails
            (scope,business_id,currency,daily_cap_minor,monthly_cap_minor,budget_floor_minor,
             budget_ceiling_minor,max_step_pct,max_changes_per_entity_per_day)
            VALUES('business',:business,'EUR',3000,90000,0,3000,30,10)"""),
            {"business": business.value},
        )
        for proposal_id in ids:
            proposal = await cases.proposals.get(proposal_id)
            await cases.submit_approval.execute(
                SubmitApprovalCommand(
                    proposal_id=proposal_id,
                    diff_hash=proposal.diff.diff_hash,
                    approved_by="test-owner",
                    channel=AuthorizationChannel.PANEL,
                )
            )
        assert (await session.execute(text("SELECT count(*) FROM ad_entities"))).scalar_one() == 0
        await session.commit()
    container.clock = FixedClock(NOW + timedelta(minutes=1))
    return ids


@pytest.mark.parametrize("same_account", [True, False])
async def test_real_account_creation_reserves_shared_budget(
    isolated_database_url: str, same_account: bool
):
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    platform = _ControlledPlatform(hold_first=True)
    container.ads_platform_port = platform
    try:
        first_id, second_id = await seed_creations(container, same_account=same_account)
        first = asyncio.create_task(_run(container, first_id))
        try:
            await asyncio.wait_for(platform.first_started.wait(), timeout=5)
            second = await asyncio.wait_for(_run(container, second_id), timeout=5)
        finally:
            platform.release_first.set()
            result = await first
        assert result == ExecutionStatus.EXECUTED
        assert second == (
            ExecutionStatus.BLOCKED_GUARDRAIL if same_account else ExecutionStatus.EXECUTED
        )
        async with container.session_factory() as session:
            assert (
                await session.execute(text("SELECT sum(delta_minor) FROM spend_ledger"))
            ).scalar_one() == (2000 if same_account else 4000)
            assert (
                await session.execute(text("SELECT count(*) FROM ad_entities"))
            ).scalar_one() == 0
            assert (
                await session.execute(
                    text("SELECT count(*) FROM executions WHERE target_account_id IS NOT NULL")
                )
            ).scalar_one() == 2
    finally:
        await container.aclose()


async def test_unknown_creation_keeps_budget_after_restart_and_receipt_only(
    isolated_database_url: str,
):
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    container.clock = FixedClock(NOW)
    platform = _ControlledPlatform(fail_first=True)
    container.ads_platform_port = platform
    try:
        first_id, second_id = await seed_creations(container)
        assert await _run(container, first_id) == ExecutionStatus.UNKNOWN
    finally:
        await container.aclose()
    restarted = Container.build(settings)
    restarted.ads_platform_port = platform
    restarted.clock = FixedClock(NOW + timedelta(minutes=7))
    try:
        assert await _run(restarted, second_id) == ExecutionStatus.BLOCKED_GUARDRAIL
        restarted.clock = FixedClock(NOW + timedelta(minutes=16))
        assert await _run(restarted, first_id) == ExecutionStatus.EXECUTED
        assert len(platform.mutations) == 1
        assert platform.receipt_reads == 1
        async with restarted.session_factory() as session:
            assert (
                await session.execute(
                    text(
                        "SELECT positive_delta_minor FROM execution_reservations "
                        "WHERE state='SETTLED'"
                    )
                )
            ).scalar_one() == 2000
            assert (
                await session.execute(text("SELECT sum(delta_minor) FROM spend_ledger"))
            ).scalar_one() == 2000
    finally:
        await restarted.aclose()


@pytest.mark.parametrize("partial", [False, True])
async def test_real_socket_broker_receipt_path(
    isolated_database_url: str, tmp_path: Path, partial: bool
):
    class NativeClient:
        calls = 0

        def campaign_creation_currency(self, account):
            assert account == "123"
            return "EUR"

        def create_paused_campaign(self, account, payload):
            self.calls += 1
            assert payload["creation_plan"]["status"] == "PAUSED"
            if partial:
                raise TimeoutError("acknowledgement lost")
            return {"campaign_resource": f"customers/{account}/campaigns/456", "status": "PAUSED"}

    native = NativeClient()
    async with _running_broker(tmp_path, search_client=native, customer_id="123") as socket:
        settings = build_api_settings(
            database_url=isolated_database_url,
            broker_socket_path=str(socket),
            approval_signing_key=_APPROVAL_SEED_B64,
        )
        container = Container.build(settings)
        container.clock = FixedClock(NOW)
        try:
            first, second = await seed_creations(container)
            expected = ExecutionStatus.UNKNOWN if partial else ExecutionStatus.EXECUTED
            assert await _run(container, first) == expected
            assert await _run(container, second) == ExecutionStatus.BLOCKED_GUARDRAIL
            container.clock = FixedClock(NOW + timedelta(minutes=16))
            # UNKNOWN stays UNKNOWN on read-only receipt; success is not requeued.
            assert await _run(container, first) == (ExecutionStatus.UNKNOWN if partial else None)
            assert native.calls == 1
            async with container.session_factory() as session:
                active = (
                    await session.execute(
                        text("SELECT count(*) FROM execution_reservations WHERE state='ACTIVE'")
                    )
                ).scalar_one()
                assert active == int(partial)
        finally:
            await container.aclose()
