"""Multiple OAuth routes must not multiply physical spend or opportunities."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from safent_ads.accounts.infrastructure.sql_repositories import SqlAccountRepository
from safent_ads.mcp.infrastructure.sql_portfolio_read_port import (
    _SELECT_FRESHNESS_PER_ACCOUNT as MCP_FRESHNESS,
)
from safent_ads.metrics.infrastructure.sql_repositories import SqlMetricFactRepository
from safent_ads.notifications.infrastructure.sql_business_status_reader import (
    _SELECT_FRESHNESS_PER_ACCOUNT as NOTIFICATION_FRESHNESS,
)
from safent_ads.opportunities.infrastructure.sql_repositories import (
    SqlCalendarEventGapPort,
    SqlCampaignProposalPort,
)
from safent_ads.orchestration.infrastructure.live_steps import LiveIngestionStep
from safent_ads.panel.infrastructure.sql_read_model import (
    _SELECT_FRESHNESS_PER_ACCOUNT as PANEL_FRESHNESS,
)
from safent_ads.proposals.domain.proposal import ProposalInvariantError
from safent_ads.rules.infrastructure.read_models.caps_and_pacing import spend_in_range_minor
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.metrics.conftest import build_fact
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.integration.execution.test_physical_controls_sql import (
    child_with_same_remote_id,
    clone_connection,
)
from tests.integration.opportunities.test_sql_repositories import (
    _NOW,
    _brief,
    _seed_offering_and_calendar_event,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("query", [MCP_FRESHNESS, PANEL_FRESHNESS, NOTIFICATION_FRESHNESS])
async def test_physical_freshness_does_not_require_duplicate_ingestion(
    db_session: AsyncSession,
    query: TextClause,
) -> None:
    original = campaign_ref(f"physical-freshness-{uuid4().hex}", "google")
    business = await seed_entity(db_session, original)
    await clone_connection(db_session, original)
    outsider, _ = await clone_connection(db_session, original, other_business=True)
    await seed_metric(db_session, original, 100, age=1)
    await seed_metric(db_session, outsider, 100)
    rows = (await db_session.execute(query, {"business_id": business})).mappings().all()
    assert len(rows) == 2
    assert {row["last_ingested_at"] for row in rows} == {_NOW - timedelta(hours=1)}


async def seed_metric(session: AsyncSession, ref: EntityRef, spend: int, *, age: int = 0) -> None:
    await session.execute(
        text("""INSERT INTO metrics_daily
        (business_id,entity_ref,entity_level,platform_account_id,stat_date,
         account_timezone,currency,spend,ingested_at)
        SELECT business_id,entity_ref,level,platform_account_id,:day,
               'Europe/Madrid','EUR',:spend,:at
        FROM ad_entities WHERE entity_ref=:ref"""),
        {"ref": str(ref), "day": _NOW.date(), "spend": spend, "at": _NOW - timedelta(hours=age)},
    )


async def test_spend_counts_latest_physical_observation_once_preserving_history(
    db_session: AsyncSession,
) -> None:
    original = campaign_ref("physical-observation", "google")
    business = await seed_entity(db_session, original)
    sibling, _ = await clone_connection(db_session, original)
    unrelated, _ = await clone_connection(db_session, original, other_business=True)
    await seed_metric(db_session, original, 4200, age=1)
    await seed_metric(db_session, sibling, 4300)
    await seed_metric(db_session, unrelated, 9900)
    assert (
        await spend_in_range_minor(
            db_session, business_id=business, start=_NOW.date(), end=_NOW.date()
        )
        == 4300
    )
    assert (
        await db_session.execute(
            text("SELECT count(*) FROM metrics_daily WHERE entity_ref=ANY(:refs)"),
            {"refs": [str(original), str(sibling), str(unrelated)]},
        )
    ).scalar_one() == 3


async def test_same_calendar_opportunity_across_connections_keeps_original_provenance(
    db_session: AsyncSession,
) -> None:
    original = campaign_ref("physical-opportunity", "google")
    business = BusinessId(await seed_entity(db_session, original))
    _, first_account = await clone_connection(db_session, original)
    _, second_account = await clone_connection(db_session, original)
    offering, event = await _seed_offering_and_calendar_event(
        db_session, business_id=business, suffix="physical-opportunity"
    )
    port = SqlCampaignProposalPort(db_session, clock=FixedClock(_NOW))
    outcomes = []
    for raw in (first_account, second_account):
        ref = EntityRef.parse(raw)
        outcomes.append(
            await port.accept(
                business_id=business,
                account_ref=ref,
                candidate_key=f"calendar_event:{event}:{ref}",
                brief=_brief(offering, event),
                expected_contribution_delta=None,
                cause_sentence="Physical opportunity",
                now=_NOW,
            )
        )
    assert outcomes[0].proposal_id == outcomes[1].proposal_id
    assert (
        await db_session.execute(
            text("SELECT entity_ref FROM proposals WHERE id=:id"), {"id": outcomes[0].proposal_id}
        )
    ).scalar_one() == first_account
    gaps = await SqlCalendarEventGapPort(db_session).list_gaps(
        business_id=business, today=_NOW.date(), horizon_days=30
    )
    assert len(gaps) == 1


async def test_campaigns_add_but_child_levels_and_other_business_do_not(
    db_session: AsyncSession,
) -> None:
    original = campaign_ref("physical-levels", "google")
    business = await seed_entity(db_session, original)
    sibling, _ = await clone_connection(db_session, original)
    other_account, _ = await clone_connection(db_session, original, other_account=True)
    other_business, _ = await clone_connection(db_session, original, other_business=True)
    child = await child_with_same_remote_id(db_session, sibling)
    distinct_campaign = (
        await db_session.execute(
            text("""INSERT INTO ad_entities
        (business_id,platform_account_id,platform,level,external_id,name,status,
         platform_state_hash,connection_id)
        SELECT business_id,platform_account_id,platform,level,external_id||'-different',
               name,status,platform_state_hash,connection_id
        FROM ad_entities WHERE entity_ref=:ref RETURNING entity_ref"""),
            {"ref": str(sibling)},
        )
    ).scalar_one()
    for ref, spend in (
        (original, 400),
        (sibling, 400),
        (other_account, 700),
        (child, 400),
        (other_business, 10000),
        (EntityRef.parse(distinct_campaign), 300),
    ):
        await seed_metric(db_session, ref, spend)
    assert (
        await spend_in_range_minor(
            db_session, business_id=business, start=_NOW.date(), end=_NOW.date()
        )
        == 1400
    )


async def test_matching_remote_ids_on_two_platforms_are_not_deduplicated(
    db_session: AsyncSession,
) -> None:
    original = campaign_ref("physical-platform", "google")
    business = await seed_entity(db_session, original)
    credential = (
        await db_session.execute(
            text("""INSERT INTO credential_refs(platform,alias)
        VALUES('meta','physical-platform-test') RETURNING id""")
        )
    ).scalar_one()
    account = (
        await db_session.execute(
            text("""INSERT INTO platform_accounts
        (business_id,platform,external_account_id,currency,timezone,api_tier,credential_ref_id,status)
        SELECT business_id,'meta',external_account_id,currency,timezone,
               'meta_full',:credential,'ACTIVE'
        FROM platform_accounts WHERE business_id=:business RETURNING id"""),
            {"business": business, "credential": credential},
        )
    ).scalar_one()
    ref = (
        await db_session.execute(
            text("""INSERT INTO ad_entities
        (business_id,platform_account_id,platform,level,external_id,name,status,platform_state_hash)
        SELECT business_id,:account,'meta',level,external_id,name,status,platform_state_hash
        FROM ad_entities WHERE entity_ref=:ref RETURNING entity_ref"""),
            {"account": account, "ref": str(original)},
        )
    ).scalar_one()
    await seed_metric(db_session, original, 100)
    await seed_metric(db_session, EntityRef.parse(ref), 200)
    assert (
        await spend_in_range_minor(
            db_session, business_id=business, start=_NOW.date(), end=_NOW.date()
        )
        == 300
    )


async def test_ties_hourly_and_suspension_preserve_observation_source(
    db_session: AsyncSession,
) -> None:
    original = campaign_ref("physical-ties", "google")
    business = await seed_entity(db_session, original)
    sibling, account = await clone_connection(db_session, original)
    repo = SqlMetricFactRepository(db_session)
    for hour in (None, 3, 4):
        await repo.upsert_many(
            [
                build_fact(original, stat_hour=hour, spend_minor=100),
                build_fact(sibling, stat_hour=hour, spend_minor=200),
            ]
        )
    await db_session.execute(
        text("UPDATE platform_accounts SET status='SUSPENDED' WHERE account_ref=:ref"),
        {"ref": account},
    )
    daily = (
        await db_session.execute(
            text("SELECT entity_ref,spend FROM metrics_daily_physical WHERE business_id=:business"),
            {"business": business},
        )
    ).one()
    assert daily == (min(str(original), str(sibling)), 100 if str(original) < str(sibling) else 200)
    hourly = (
        await db_session.execute(
            text(
                "SELECT entity_ref,stat_hour FROM metrics_hourly_physical "
                "WHERE business_id=:business ORDER BY stat_hour"
            ),
            {"business": business},
        )
    ).all()
    assert hourly == [(daily.entity_ref, 3), (daily.entity_ref, 4)]
    # Source-specific detail and audit retain both observations.
    assert (
        await repo.find_by_natural_key(
            entity_ref=sibling, stat_date=build_fact(sibling).stat_date, stat_hour=None
        )
    ).spend_minor == 200


async def test_observation_selector_stable_and_status_scoped(db_session: AsyncSession) -> None:
    original = campaign_ref("physical-selector", "google")
    business = BusinessId(await seed_entity(db_session, original))
    await clone_connection(db_session, original)
    await clone_connection(db_session, original, other_business=True)
    repo = SqlAccountRepository(db_session)
    first = await repo.list_for_observation(business)
    assert len(first) == 1
    assert [a.account_ref for a in await repo.list_for_observation(business)] == [
        first[0].account_ref
    ]
    await db_session.execute(
        text("UPDATE platform_accounts SET status='SUSPENDED' WHERE account_ref=:ref"),
        {"ref": str(first[0].account_ref)},
    )
    remaining = await repo.list_for_observation(business)
    assert len(remaining) == 1
    assert remaining[0].account_ref != first[0].account_ref
    await db_session.execute(
        text("UPDATE platform_accounts SET status='SUSPENDED' WHERE business_id=:id"),
        {"id": business.value},
    )
    assert await repo.list_for_observation(business) == []


async def test_concurrent_opportunities_and_restart_do_not_switch_provenance(
    isolated_database_url: str,
) -> None:
    engine = create_async_engine(isolated_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            original = campaign_ref(f"physical-concurrent-{uuid4().hex}", "google")
            business = BusinessId(await seed_entity(session, original))
            _, a = await clone_connection(session, original)
            _, b = await clone_connection(session, original)
            offering, event = await _seed_offering_and_calendar_event(
                session, business_id=business, suffix=uuid4().hex
            )
            await session.commit()

        async def propose(raw: str):  # noqa: ANN202 - local task result
            async with factory() as session:
                ref = EntityRef.parse(raw)
                result = await SqlCampaignProposalPort(session, clock=FixedClock(_NOW)).accept(
                    business_id=business,
                    account_ref=ref,
                    candidate_key=f"calendar_event:{event}:{ref}",
                    brief=_brief(offering, event),
                    expected_contribution_delta=None,
                    cause_sentence="Concurrent source",
                    now=_NOW,
                )
                await session.commit()
                return result

        outcomes = await asyncio.gather(propose(a), propose(b))
        assert outcomes[0].proposal_id == outcomes[1].proposal_id
        async with factory() as session:
            snapshot = (
                await session.execute(
                    text("SELECT entity_ref,diff_hash FROM proposals WHERE id=:id"),
                    {"id": outcomes[0].proposal_id},
                )
            ).one()
            # Revoking its route does not authorize reparenting to another OAuth.
            await session.execute(
                text("UPDATE platform_accounts SET status='SUSPENDED' WHERE account_ref=:ref"),
                {"ref": snapshot.entity_ref},
            )
            await session.commit()
        restarted = await propose(b if snapshot.entity_ref == a else a)
        assert restarted.proposal_id == outcomes[0].proposal_id
        assert restarted.diff_hash == snapshot.diff_hash
    finally:
        await engine.dispose()


async def test_opportunity_spoofed_business_cannot_claim_physical_scope(
    db_session: AsyncSession,
) -> None:
    original = campaign_ref("physical-opportunity-scope", "google")
    business = BusinessId(await seed_entity(db_session, original))
    _, raw = await clone_connection(db_session, original)
    offering, event = await _seed_offering_and_calendar_event(
        db_session, business_id=business, suffix="physical-spoof"
    )
    with pytest.raises(ProposalInvariantError, match="scope_mismatch"):
        await SqlCampaignProposalPort(db_session, clock=FixedClock(_NOW)).accept(
            business_id=business,
            account_ref=replace(EntityRef.parse(raw), business_id=uuid4()),
            candidate_key="spoof",
            brief=_brief(offering, event),
            expected_contribution_delta=None,
            cause_sentence="Not authorized",
            now=_NOW,
        )


class _DeniedMetrics:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_account_inventory(self, account_ref):  # noqa: ANN001, ANN201
        self.calls.append(str(account_ref))
        raise PermissionError("revoked_test_credential")


async def test_concurrent_ingestion_replays_survive_restart_without_double_count(
    isolated_database_url: str,
) -> None:
    engine = create_async_engine(isolated_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            original = campaign_ref(f"physical-metrics-race-{uuid4().hex}", "google")
            business = await seed_entity(session, original)
            sibling, _ = await clone_connection(session, original)
            await session.commit()

        async def ingest(ref: EntityRef) -> None:
            newest = replace(build_fact(ref, spend_minor=4200), ingested_at=_NOW)
            async with factory() as session:
                await SqlMetricFactRepository(session).upsert_many([newest, newest])
                await session.commit()
            async with factory() as session:
                await SqlMetricFactRepository(session).upsert_many(
                    [replace(newest, spend_minor=100, ingested_at=_NOW - timedelta(hours=1))]
                )
                await session.commit()

        await asyncio.gather(ingest(original), ingest(sibling))
        await engine.dispose()  # no in-memory cache can be responsible for deduplication
        async with factory() as session:
            day = build_fact(original).stat_date
            assert (
                await spend_in_range_minor(session, business_id=business, start=day, end=day)
                == 4200
            )
            assert (
                await session.execute(
                    text("SELECT count(*) FROM metrics_daily WHERE business_id=:business"),
                    {"business": business},
                )
            ).scalar_one() == 2
    finally:
        await engine.dispose()


async def test_revoked_observation_never_falls_back_to_another_oauth(
    isolated_database_url: str,
) -> None:
    engine = create_async_engine(isolated_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            original = campaign_ref(f"physical-denied-{uuid4().hex}", "google")
            business = BusinessId(await seed_entity(session, original))
            await clone_connection(session, original)
            await session.commit()
        denied = _DeniedMetrics()
        for _ in range(2):
            with pytest.raises(PermissionError, match="revoked_test_credential"):
                await LiveIngestionStep(factory, denied, FixedClock(_NOW)).run(
                    business, str(uuid4()), _NOW
                )
        assert len(denied.calls) == 2 and denied.calls[0] == denied.calls[1]
    finally:
        await engine.dispose()
