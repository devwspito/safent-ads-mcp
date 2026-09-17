"""Exact SQL account boundary, before pagination/metrics; no managed activation."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from tests.integration.execution.test_physical_controls_sql import (
    child_with_same_remote_id,
    clone_connection,
)
from tests.integration.mcp.test_entity_read_port_contract import _NOW, _WINDOW, _seed_sql_entity

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.sql_entity_read_port import SqlEntityReadPort
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityRef

pytestmark = pytest.mark.integration


@pytest.fixture
async def scoped_entities(mcp_session_factory):
    business, legacy = await _seed_sql_entity(mcp_session_factory)
    async with mcp_session_factory() as session:
        a, account_a = await clone_connection(session, EntityRef.parse(legacy))
        b, account_b = await clone_connection(session, EntityRef.parse(legacy))
        # Another physical account within A's SAME OAuth connection is also
        # outside A's permission; matching connection alone is insufficient.
        other_id = uuid4()
        account_other = (
            await session.execute(
                text("""
            INSERT INTO platform_accounts
              (id,business_id,platform,external_account_id,currency,timezone,api_tier,
               credential_ref_id,status,connection_id)
            SELECT :id,business_id,platform,external_account_id || '-other',currency,
                   timezone,api_tier,credential_ref_id,status,connection_id
            FROM platform_accounts WHERE account_ref=:account
            RETURNING account_ref
        """),
                {"id": other_id, "account": account_a},
            )
        ).scalar_one()
        other = EntityRef.parse(
            (
                await session.execute(
                    text("""
            INSERT INTO ad_entities
              (business_id,platform_account_id,platform,level,external_id,name,status,
               platform_state_hash,connection_id)
            SELECT business_id,:account,platform,'campaign',external_id || '-other-account',
                   'Other account',status,platform_state_hash,connection_id
            FROM ad_entities WHERE entity_ref=:ref RETURNING entity_ref
        """),
                    {"account": other_id, "ref": str(a)},
                )
            ).scalar_one()
        )
        child_a = await child_with_same_remote_id(session, a)
        child_b = await child_with_same_remote_id(session, b)
        # Three campaigns in A; many unrelated rows must not consume page slots.
        for ref, prefix, count in ((a, "allowed", 2), (b, "unrelated", 5)):
            for number in range(count):
                await session.execute(
                    text("""
                    INSERT INTO ad_entities
                      (business_id,platform_account_id,platform,level,external_id,name,status,
                       platform_state_hash,connection_id)
                    SELECT business_id,platform_account_id,platform,'campaign',:remote,:name,
                           status,platform_state_hash,connection_id
                    FROM ad_entities WHERE entity_ref=:ref
                """),
                    {"remote": f"{prefix}-{number}", "name": prefix, "ref": str(ref)},
                )
        for ref, amount in ((a, 100), (b, 9900), (other, 8800)):
            await session.execute(
                text("""
                INSERT INTO metrics_daily
                  (business_id,entity_ref,entity_level,platform_account_id,stat_date,
                   account_timezone,currency,spend,impressions,clicks,conversions_lead,
                   conversion_value,ingested_at)
                SELECT business_id,entity_ref,'campaign',platform_account_id,:day,
                       'Europe/Madrid','EUR',:amount,100,10,2,0,:now
                FROM ad_entities WHERE entity_ref=:ref
            """),
                {"ref": str(ref), "day": _NOW.date(), "now": _NOW, "amount": amount},
            )
        await session.commit()
    ports = [
        SqlEntityReadPort(
            mcp_session_factory, FixedClock(_NOW), account_scope=AccountRef.parse(ref)
        )
        for ref in (account_a, account_b, account_other)
    ]
    return str(business), (a, b, other), (child_a, child_b), ports


async def test_account_filter_precedes_page_limit_and_cursors(scoped_entities):
    business, (a, _, _), _, (port, _, _) = scoped_entities
    found, cursor = [], None
    for _ in range(4):
        page = await port.list_campaigns(
            business,
            platform=None,
            status=None,
            limit=1,
            cursor=cursor,
        )
        found.extend(item.entity_ref for item in page.items)
        if page.cursor is None:
            break
        cursor = page.cursor
    assert len(found) == 3
    assert len(set(found)) == 3
    assert all(EntityRef.parse(ref).connection_id == a.connection_id for ref in found)
    assert str(a) in found


@pytest.mark.parametrize("operation", ["campaign", "children", "daily", "hourly", "insights"])
@pytest.mark.parametrize("target", [1, 2])
async def test_same_business_other_connection_or_account_is_not_found(
    scoped_entities, operation, target
):
    business, refs, _, (port, _, _) = scoped_entities
    ref = str(refs[target])
    with pytest.raises(EntityNotFoundError):
        if operation == "campaign":
            await port.get_campaign(business, ref)
        elif operation == "children":
            await port.list_children(business, ref, limit=10, cursor=None)
        elif operation == "insights":
            await port.get_insights(business, ref, window=_WINDOW, breakdown=None)
        else:
            await port.get_entity_metrics(business, ref, window=_WINDOW, granularity=operation)


async def test_children_and_metrics_use_only_the_exact_authorized_connection(scoped_entities):
    business, (a, _, _), (child, _), (port, _, _) = scoped_entities
    page = await port.list_children(business, str(a), limit=10, cursor=None)
    assert [entry.entity_ref for entry in page.items] == [str(child)]
    metrics = await port.get_entity_metrics(business, str(a), window=_WINDOW, granularity="daily")
    assert len(metrics.points) == 1
    assert metrics.points[0].spend.amount == 1
    insights = await port.get_insights(business, str(a), window=_WINDOW, breakdown=None)
    assert insights.breakdown == {
        "lead": 100.0,
        "whatsapp": 0.0,
        "call": 0.0,
        "business_conversion": 0.0,
    }


async def test_concurrent_ports_do_not_share_account_scope(scoped_entities):
    business, (a, b, _), _, (port_a, port_b, _) = scoped_entities
    campaigns = await asyncio.gather(
        port_a.get_campaign(business, str(a)),
        port_b.get_campaign(business, str(b)),
    )
    assert [entry.entity_ref for entry in campaigns] == [str(a), str(b)]
    results = await asyncio.gather(
        port_a.get_campaign(business, str(b)),
        port_b.get_campaign(business, str(a)),
        return_exceptions=True,
    )
    assert all(isinstance(result, EntityNotFoundError) for result in results)


async def test_cross_business_denied_even_when_request_names_bound_entity(scoped_entities):
    _, (a, _, _), _, (port, _, _) = scoped_entities
    with pytest.raises(EntityNotFoundError):
        await port.get_campaign(str(uuid4()), str(a))


async def test_foreign_cursor_cannot_be_reused_for_another_account(scoped_entities):
    business, (_, b, _), _, (port, _, _) = scoped_entities
    with pytest.raises(EntityNotFoundError):
        await port.list_campaigns(
            business, platform=None, status=None, limit=10, cursor=str(b),
        )


def test_legacy_account_scope_is_not_a_managed_selector(mcp_session_factory):
    with pytest.raises(ValueError, match="business and connection"):
        SqlEntityReadPort(mcp_session_factory, account_scope=AccountRef.parse("google:123"))
