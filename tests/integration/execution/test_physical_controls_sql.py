"""Safety controls belong to physical assets, not the OAuth route to them."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.ports import EntityStateSnapshot
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.composition.container import Container
from safent_ads.execution.application.toggle_emergency_brake import BrakeAlreadyEngagedError
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
    GuardrailScope,
    ScopeKind,
)
from safent_ads.execution.infrastructure.errors import UnknownEntityRefError
from safent_ads.execution.infrastructure.sql_brake_state import SqlBrakeStatePort
from safent_ads.execution.infrastructure.sql_guardrail_sets import SqlGuardrailSetRepository
from safent_ads.proposals.application.submit_approval import SubmitApprovalCommand
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.rules.domain.emergency_brake import BrakeMode as RuleBrakeMode
from safent_ads.rules.domain.emergency_brake import BrakeScope as RuleBrakeScope
from safent_ads.rules.domain.emergency_brake import BrakeScopeKind as RuleBrakeScopeKind
from safent_ads.rules.domain.emergency_brake import EmergencyBrake as RuleEmergencyBrake
from safent_ads.rules.infrastructure.sql_repositories import SqlEmergencyBrakeRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef
from tests.conftest import BusinessFactory, OwnerFactory
from tests.contracts.execution.conftest import (
    NOW,
    GuardrailLimits,
    seed_authorized_proposal,
    seed_guardrails,
)
from tests.integration.composition.test_write_path_end_to_end import _raise_budget_proposal
from tests.integration.execution.test_chokepoint_budget_race_regression import (
    _STATE,
    _STATE_HASH,
    _ControlledPlatform,
    _run,
)
from tests.integration.execution.test_chokepoint_budget_race_regression import (
    isolated_database_url as isolated_database_url,  # noqa: PLC0414 - re-export pytest fixture
)
from tests.integration.execution.test_spend_ledger_sql import (
    TODAY,
    entity_scope,
    given_applied_change,
    ledger_for,
)
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration


async def clone_connection(
    session: AsyncSession,
    original: EntityRef,
    *,
    other_business: bool = False,
    other_account: bool = False,
) -> tuple[EntityRef, str]:
    owner = await OwnerFactory(session).create()
    source = (
        await session.execute(
            text("SELECT business_id FROM ad_entities WHERE entity_ref=:ref"),
            {"ref": str(original)},
        )
    ).scalar_one()
    business = await BusinessFactory(session).create() if other_business else source
    connection, account = uuid4(), uuid4()
    await session.execute(
        text("""INSERT INTO platform_connections(id,business_id,owner_id,platform)
        VALUES(:connection,:business,:owner,:platform)"""),
        {
            "connection": connection,
            "business": business,
            "owner": owner,
            "platform": original.platform.value,
        },
    )
    await session.execute(
        text("""INSERT INTO platform_accounts
        (id,business_id,platform,external_account_id,currency,timezone,api_tier,
         credential_ref_id,status,connection_id)
        SELECT :account,:business,a.platform,
               CASE WHEN :other_account THEN a.external_account_id || '-other'
                    ELSE a.external_account_id END,
               a.currency,a.timezone,a.api_tier,a.credential_ref_id,'ACTIVE',:connection
          FROM platform_accounts a JOIN ad_entities e ON e.platform_account_id=a.id
         WHERE e.entity_ref=:ref"""),
        {
            "account": account,
            "business": business,
            "connection": connection,
            "ref": str(original),
            "other_account": other_account,
        },
    )
    await session.execute(
        text("""INSERT INTO ad_entities
        (business_id,platform_account_id,platform,level,external_id,name,status,
         platform_state_hash,connection_id)
        SELECT :business,:account,platform,level,external_id,name,status,
               platform_state_hash,:connection FROM ad_entities WHERE entity_ref=:ref"""),
        {"business": business, "account": account, "connection": connection, "ref": str(original)},
    )
    account_ref = (
        await session.execute(
            text("SELECT account_ref FROM platform_accounts WHERE id=:id"), {"id": account}
        )
    ).scalar_one()
    return replace(original, business_id=business, connection_id=connection), account_ref


def brake_scope(ref: EntityRef) -> BrakeScope:
    return BrakeScope(kind=BrakeScopeKind.PLATFORM_ACCOUNT, ref=str(ref))


async def child_with_same_remote_id(session: AsyncSession, parent: EntityRef) -> EntityRef:
    # Meta IDs are normally global; deliberately reuse one across levels to
    # prove the safety identity does not depend on that provider convention.
    await session.execute(
        text("""INSERT INTO ad_entities
        (business_id,platform_account_id,platform,level,external_id,parent_id,name,status,
         platform_state_hash,connection_id)
        SELECT business_id,platform_account_id,platform,'ad_set',external_id,id,name,status,
               platform_state_hash,connection_id FROM ad_entities WHERE entity_ref=:ref"""),
        {"ref": str(parent)},
    )
    return replace(parent, level=EntityLevel.AD_SET)


async def test_brake_crosses_connections_not_business_or_remote_account(
    db_session: AsyncSession,
) -> None:
    original = await seed_authorized_proposal(db_session)
    a, account_a = await clone_connection(db_session, original.entity_ref)
    b, account_b = await clone_connection(db_session, original.entity_ref)
    other_business, _ = await clone_connection(db_session, original.entity_ref, other_business=True)
    other_account, _ = await clone_connection(db_session, original.entity_ref, other_account=True)
    brakes = SqlBrakeStatePort(db_session)
    brake = EmergencyBrake(scope=brake_scope(a), mode=BrakeMode.ALL)
    brake.engage("Incident on the physical account", NOW)
    await brakes.save(brake)
    observed = await brakes.get(brake_scope(b))
    assert observed is not None and observed.engaged and observed.mode is BrakeMode.ALL
    assert await brakes.get(brake_scope(other_business)) is None
    assert await brakes.get(brake_scope(other_account)) is None
    separate = EmergencyBrake(scope=brake_scope(other_business), mode=BrakeMode.ALL)
    separate.engage("Independent business incident", NOW)
    await brakes.save(separate)
    with pytest.raises(UnknownEntityRefError):
        await brakes.get(brake_scope(replace(b, business_id=other_business.business_id)))
    rule_brakes = SqlEmergencyBrakeRepository(db_session)
    scope_b = RuleBrakeScope(
        kind=RuleBrakeScopeKind.PLATFORM_ACCOUNT, platform_account_ref=account_b
    )
    assert await rule_brakes.find_active(scope=scope_b) is not None
    # Revoking/disabling the route carrying the original brake cannot reopen B.
    await db_session.execute(
        text("UPDATE platform_accounts SET status='SUSPENDED' WHERE account_ref=:ref"),
        {"ref": account_a},
    )
    assert (await brakes.get(brake_scope(b))).engaged
    await rule_brakes.release(scope=scope_b, released_by="authorized-owner", released_at=NOW)
    assert not (await brakes.get(brake_scope(a))).engaged
    assert not (await brakes.get(brake_scope(b))).engaged
    assert (await brakes.get(brake_scope(other_business))).engaged


async def test_applied_frequency_survives_another_connection_and_revocation(
    db_session: AsyncSession,
) -> None:
    original = await seed_authorized_proposal(db_session)
    a, _ = await clone_connection(db_session, original.entity_ref)
    b, _ = await clone_connection(db_session, original.entity_ref)
    other_business, _ = await clone_connection(db_session, original.entity_ref, other_business=True)
    other_account, _ = await clone_connection(db_session, original.entity_ref, other_account=True)
    await given_applied_change(db_session, original, day=TODAY, delta_minor=100)
    ledger = ledger_for(db_session)
    for ref in (a, b):
        snapshot = await ledger.snapshot(entity_scope(ref), ref)
        assert snapshot.changes_count_today_for_entity == 1
        assert snapshot.applied_changes_today.amount == 1
    for ref in (other_business, other_account):
        assert (await ledger.snapshot(entity_scope(ref), ref)).changes_count_today_for_entity == 0
    child = await child_with_same_remote_id(db_session, b)
    assert (await ledger.snapshot(entity_scope(child), child)).changes_count_today_for_entity == 0
    await db_session.execute(
        text("""UPDATE platform_accounts SET status='SUSPENDED'
        WHERE id=(SELECT platform_account_id FROM ad_entities WHERE entity_ref=:ref)"""),
        {"ref": str(original.entity_ref)},
    )
    with pytest.raises(UnknownEntityRefError):
        spoof = replace(b, business_id=other_business.business_id)
        await ledger.snapshot(entity_scope(spoof), spoof)
    assert (await ledger.snapshot(entity_scope(b), b)).changes_count_today_for_entity == 1


async def test_campaign_frequency_policy_reaches_its_physical_siblings(
    db_session: AsyncSession,
) -> None:
    original = await seed_authorized_proposal(db_session)
    a, _ = await clone_connection(db_session, original.entity_ref)
    b, _ = await clone_connection(db_session, original.entity_ref)
    await seed_guardrails(
        db_session, scope=entity_scope(a), limits=GuardrailLimits(), level="business"
    )
    await seed_guardrails(
        db_session,
        scope=entity_scope(a),
        limits=GuardrailLimits(max_changes_per_entity_day=1),
        level="campaign",
    )
    effective = await SqlGuardrailSetRepository(db_session).get_effective(
        GuardrailScope(kind=ScopeKind.ENTITY, ref=str(b))
    )
    assert effective.max_changes_per_entity_day == 1
    child = await child_with_same_remote_id(db_session, b)
    inherited = await SqlGuardrailSetRepository(db_session).get_effective(entity_scope(child))
    assert inherited.max_changes_per_entity_day == 1


async def test_legacy_duplicate_brakes_stay_strict_until_explicit_physical_release(
    db_session: AsyncSession,
) -> None:
    original = await seed_authorized_proposal(db_session)
    a, account_a = await clone_connection(db_session, original.entity_ref)
    b, account_b = await clone_connection(db_session, original.entity_ref)
    for account, mode in ((account_a, "ALL"), (account_b, "AUTONOMOUS")):
        # Existing databases may already contain both rows. Do not rewrite
        # history or select the most recently connected route as authoritative.
        await db_session.execute(
            text("""INSERT INTO emergency_brakes
            (scope_kind,platform_account_id,mode,reason,engaged_by,engaged_at)
            SELECT 'platform_account',id,:mode,'Old incident','owner',:now
              FROM platform_accounts WHERE account_ref=:ref"""),
            {"ref": account, "mode": mode, "now": NOW},
        )
    brakes = SqlBrakeStatePort(db_session)
    observed = await brakes.get(brake_scope(b))
    assert observed is not None and observed.mode is BrakeMode.ALL and observed.engaged
    rules = SqlEmergencyBrakeRepository(db_session)
    rule_scope = RuleBrakeScope(
        kind=RuleBrakeScopeKind.PLATFORM_ACCOUNT, platform_account_ref=account_b
    )
    assert (await rules.find_active(scope=rule_scope)).mode is RuleBrakeMode.ALL
    # An overlapping engage cannot silently downgrade ALL to AUTONOMOUS.
    weaker = EmergencyBrake(scope=brake_scope(b), mode=BrakeMode.AUTONOMOUS)
    weaker.engage("Concurrent weaker engage", NOW)
    with pytest.raises(BrakeAlreadyEngagedError):
        await brakes.save(weaker)
    assert (await brakes.get(brake_scope(a))).mode is BrakeMode.ALL
    observed.release(NOW)
    await brakes.save(observed)
    assert not (await brakes.get(brake_scope(a))).engaged
    assert await rules.find_active(scope=rule_scope) is None
    row = (
        await db_session.execute(
            text("""SELECT count(*) AS total,
        count(*) FILTER(WHERE released_at IS NULL) AS active FROM emergency_brakes
        WHERE platform_account_id IN (SELECT id FROM platform_accounts
            WHERE account_ref IN (:a,:b))"""),
            {"a": account_a, "b": account_b},
        )
    ).one()
    assert tuple(row) == (2, 0)


@pytest.mark.parametrize("unknown", [False, True])
async def test_two_oauth_routes_share_frequency_during_inflight_or_unknown(
    isolated_database_url: str,
    unknown: bool,
) -> None:
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    container.clock = FixedClock(NOW)
    platform = _ControlledPlatform(hold_first=not unknown, fail_first=unknown)
    container.ads_platform_port = platform
    try:
        async with container.session_factory() as session:
            original = await seed_authorized_proposal(session)
            a, _ = await clone_connection(session, original.entity_ref)
            b, _ = await clone_connection(session, original.entity_ref)
            await seed_guardrails(
                session,
                scope=entity_scope(a),
                level="business",
                limits=GuardrailLimits(
                    daily_cap="1000", max_step_pct=1, max_changes_per_entity_day=1
                ),
            )
            proposal_ids = []
            for ref in (a, b):
                platform.entity_states[ref] = EntityStateSnapshot(
                    entity_ref=ref,
                    status=AdEntityStatus.ACTIVE,
                    is_controllable=True,
                    canonical_state=_STATE,
                    fetched_at=NOW,
                )
                proposal = _raise_budget_proposal(
                    BusinessId(ref.business_id), ref, expected_state_hash=_STATE_HASH
                )
                cases = container.build_execution_use_cases(session)
                await cases.proposals.save(proposal)
                await cases.submit_approval.execute(
                    SubmitApprovalCommand(
                        proposal_id=proposal.proposal_id,
                        diff_hash=proposal.diff.diff_hash,
                        approved_by="test-owner",
                        channel=AuthorizationChannel.PANEL,
                    )
                )
                proposal_ids.append(proposal.proposal_id)
            await session.commit()
        container.clock = FixedClock(NOW + timedelta(minutes=1))
        first = asyncio.create_task(_run(container, proposal_ids[0]))
        try:
            await asyncio.wait_for(platform.first_started.wait(), timeout=3)
            if unknown:
                assert await first is ExecutionStatus.UNKNOWN
            second = await asyncio.wait_for(_run(container, proposal_ids[1]), timeout=3)
            assert second is ExecutionStatus.BLOCKED_GUARDRAIL
            assert len(platform.mutations) == 1
        finally:
            platform.release_first.set()
            await asyncio.wait_for(first, timeout=3)
        async with container.session_factory() as session:
            snapshot = await ledger_for(session).snapshot(entity_scope(b), b)
            assert snapshot.changes_count_today_for_entity == 1
            assert snapshot.reserved_increase.amount == (20 if unknown else 0)
            rows = (
                (await session.execute(text("SELECT state FROM execution_reservations")))
                .scalars()
                .all()
            )
            assert rows == (["ACTIVE"] if unknown else ["SETTLED"])
    finally:
        await container.aclose()


@pytest.mark.parametrize("second_channel", ["rules", "execution"])
async def test_concurrent_brake_engage_channels_share_one_physical_brake(
    isolated_database_url: str,
    second_channel: str,
) -> None:
    container = Container.build(build_api_settings(database_url=isolated_database_url))
    try:
        async with container.session_factory() as session:
            original = await seed_authorized_proposal(session)
            a, _ = await clone_connection(session, original.entity_ref)
            b, account_b = await clone_connection(session, original.entity_ref)
            await session.commit()
        first_saved, allow_commit = asyncio.Event(), asyncio.Event()

        async def first_writer() -> None:
            async with container.session_factory() as session:
                brake = EmergencyBrake(scope=brake_scope(a), mode=BrakeMode.ALL)
                brake.engage("Owner stops all writes", NOW)
                await SqlBrakeStatePort(session).save(brake)
                first_saved.set()
                await allow_commit.wait()
                await session.commit()

        async def second_writer() -> None:
            async with container.session_factory() as session:
                if second_channel == "execution":
                    brake = EmergencyBrake(scope=brake_scope(b), mode=BrakeMode.AUTONOMOUS)
                    brake.engage("Other session", NOW)
                    await SqlBrakeStatePort(session).save(brake)
                else:
                    await SqlEmergencyBrakeRepository(session).engage(
                        RuleEmergencyBrake(
                            scope=RuleBrakeScope(
                                kind=RuleBrakeScopeKind.PLATFORM_ACCOUNT,
                                platform_account_ref=account_b,
                            ),
                            mode=RuleBrakeMode.AUTONOMOUS,
                            reason="Other session",
                            engaged_by="owner",
                            engaged_at=NOW,
                        )
                    )
                await session.commit()

        first = asyncio.create_task(first_writer())
        second = None
        try:
            await asyncio.wait_for(first_saved.wait(), timeout=3)
            second = asyncio.create_task(second_writer())
            done, _ = await asyncio.wait({second}, timeout=0.05)
            assert not done, "Sibling writer must wait for the physical-account lock"
        finally:
            allow_commit.set()
            await asyncio.wait_for(first, timeout=3)
            if second is not None:
                expected = BrakeAlreadyEngagedError if second_channel == "execution" else ValueError
                with pytest.raises(expected, match="physical account already"):
                    await asyncio.wait_for(second, timeout=3)
        async with container.session_factory() as session:
            assert (
                await session.execute(
                    text("SELECT count(*) FROM emergency_brakes WHERE released_at IS NULL")
                )
            ).scalar_one() == 1
            observed = await SqlBrakeStatePort(session).get(brake_scope(b))
            assert observed is not None and observed.mode is BrakeMode.ALL and observed.engaged
    finally:
        await container.aclose()
