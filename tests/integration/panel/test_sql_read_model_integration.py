"""`SqlPanelReadPort` contra Postgres real: los agregados "dificiles" que
`FakePanelReadPort` solo simula -- gasto de hoy/MTD, indice de ritmo y
frescura -- con expectativas calculadas a mano contra hechos y
guardarrailes sembrados de verdad (`seed_entity`, mismo patron que
`tests/integration/signals/test_read_models.py`)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.panel.infrastructure.sql_read_model import SqlPanelReadPort
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.rules.infrastructure.sql_repositories import SqlGuardrailRepository
from safent_ads.shared.clock import FixedClock
from tests.conftest import rolled_back_session
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity

_INSERT_SIGNAL = text("""
    INSERT INTO signals (business_id, entity_ref, kind, strength, cause, cause_code,
                         rule_code, gate_verdicts, evidence, data_window, window_start,
                         window_end, money_at_stake_minor, money_at_stake_currency,
                         cycle_id, emitted_at)
    VALUES (:business_id, :entity_ref, :kind, 78, 'CPL 41 vs 28', 'CPL_OVER_TARGET', 'M05',
            '[]'::jsonb, '{}'::jsonb,
            '7D', :window_start, :window_end, 31000, 'EUR', :cycle_id, :emitted_at)
""")

pytestmark = pytest.mark.integration

_TODAY = date(2026, 3, 15)
_NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
_MONTH_START = date(2026, 3, 1)
_DAYS_ELAPSED = 15  # 1..15 de marzo, ambos incluidos
_DAYS_IN_MARCH = 31

_TODAY_SPEND_MINOR = 5_000  # 50,00 EUR
_EARLIER_SPEND_MINOR = 3_000  # 10 de marzo, 30,00 EUR
_MTD_SPEND_MINOR = _TODAY_SPEND_MINOR + _EARLIER_SPEND_MINOR

_MONTHLY_CAP_MINOR = 100_000  # 1.000,00 EUR
_DAILY_CAP_MINOR = 10_000


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    async with rolled_back_session(database_url) as open_session:
        yield open_session


async def _account_id_for(session: AsyncSession, business_id: uuid.UUID) -> uuid.UUID:
    result = await session.execute(
        text("SELECT id FROM platform_accounts WHERE business_id = :business_id"),
        {"business_id": business_id},
    )
    return result.scalar_one()


async def _insert_daily_fact(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    account_id: uuid.UUID,
    entity_ref: str,
    stat_date: date,
    spend_minor: int,
    conversions_lead: int,
    ingested_at: datetime,
) -> None:
    await session.execute(
        text("""
            INSERT INTO metrics_daily
                (business_id, entity_ref, entity_level, platform_account_id, stat_date,
                 account_timezone, currency, spend, impressions, clicks, conversions_lead,
                 conversion_value, ingested_at)
            VALUES
                (:business_id, :entity_ref, 'campaign', :account_id, :stat_date,
                 'Europe/Madrid', 'EUR', :spend, 100, 10, :conversions_lead, 0, :ingested_at)
        """),
        {
            "business_id": business_id,
            "entity_ref": entity_ref,
            "account_id": account_id,
            "stat_date": stat_date,
            "spend": spend_minor,
            "conversions_lead": conversions_lead,
            "ingested_at": ingested_at,
        },
    )


async def _seed_portfolio(session: AsyncSession) -> tuple[uuid.UUID, str]:
    entity_ref_vo = campaign_ref(f"camp{uuid.uuid4().hex[:8]}", platform_value="google")
    business_id = await seed_entity(session, entity_ref_vo)
    account_id = await _account_id_for(session, business_id)
    entity_ref = str(entity_ref_vo)

    await _insert_daily_fact(
        session,
        business_id=business_id,
        account_id=account_id,
        entity_ref=entity_ref,
        stat_date=_TODAY,
        spend_minor=_TODAY_SPEND_MINOR,
        conversions_lead=2,
        ingested_at=_NOW - timedelta(minutes=10),
    )
    await _insert_daily_fact(
        session,
        business_id=business_id,
        account_id=account_id,
        entity_ref=entity_ref,
        stat_date=date(2026, 3, 10),
        spend_minor=_EARLIER_SPEND_MINOR,
        conversions_lead=1,
        ingested_at=_NOW - timedelta(days=5),
    )

    await SqlGuardrailRepository(session).save_for_account(
        account_ref=f"google:{account_external_id(entity_ref_vo)}",
        policy=GuardrailPolicy(
            daily_cap_minor=_DAILY_CAP_MINOR,
            monthly_cap_minor=_MONTHLY_CAP_MINOR,
            floor_minor=0,
            ceiling_minor=_MONTHLY_CAP_MINOR,
            max_step_pct=20.0,
            max_changes_per_day=5,
        ),
        currency="EUR",
    )
    await session.flush()
    return business_id, entity_ref


async def test_portfolio_spend_today_and_mtd_match_hand_computed_sums(
    session: AsyncSession,
) -> None:
    business_id, _entity_ref = await _seed_portfolio(session)
    port = SqlPanelReadPort(session, clock=FixedClock(_NOW))

    view = await port.get_portfolio(str(business_id), window="7D")

    assert view.spend.today.amount == Decimal(_TODAY_SPEND_MINOR) / 100
    assert view.spend.mtd.amount == Decimal(_MTD_SPEND_MINOR) / 100


async def test_portfolio_pacing_index_matches_hand_computed_formula(
    session: AsyncSession,
) -> None:
    business_id, _entity_ref = await _seed_portfolio(session)
    port = SqlPanelReadPort(session, clock=FixedClock(_NOW))

    view = await port.get_portfolio(str(business_id), window="7D")

    expected_expected_spend = _MONTHLY_CAP_MINOR * _DAYS_ELAPSED / _DAYS_IN_MARCH
    expected_index_pct = round(_MTD_SPEND_MINOR / expected_expected_spend * 100, 1)
    assert view.pacing.index_pct == expected_index_pct
    assert view.pacing.days_remaining == _DAYS_IN_MARCH - _DAYS_ELAPSED
    assert view.caps.monthly is not None
    assert view.caps.monthly.amount == Decimal(_MONTHLY_CAP_MINOR) / 100
    assert view.caps.daily is not None
    assert view.caps.daily.amount == Decimal(_DAILY_CAP_MINOR) / 100
    assert view.is_partial is False


async def test_portfolio_freshness_lag_matches_hand_computed_minutes(
    session: AsyncSession,
) -> None:
    business_id, _entity_ref = await _seed_portfolio(session)
    port = SqlPanelReadPort(session, clock=FixedClock(_NOW))

    view = await port.get_portfolio(str(business_id), window="7D")

    assert view.freshness.lag_minutes == 10
    assert view.freshness.is_stale is False


async def test_get_freshness_lists_one_entry_per_platform_account(
    session: AsyncSession,
) -> None:
    business_id, _entity_ref = await _seed_portfolio(session)
    port = SqlPanelReadPort(session, clock=FixedClock(_NOW))

    freshness_items = await port.get_freshness(str(business_id))

    assert len(freshness_items) == 1
    assert freshness_items[0].lag_minutes == 10
    assert freshness_items[0].is_stale is False


async def test_portfolio_row_platform_account_id_is_the_canonical_ref_not_the_uuid(
    session: AsyncSession,
) -> None:
    """Hotfix 0.2.20 Bug C: `/portfolio` and `/platform-accounts` must key
    accounts the same way. `platform_account_id` is the canonical
    `<platform>:account:...` ref `/platform-accounts` returns; the internal
    UUID moves to the additive `platform_account_uuid` field."""
    business_id, entity_ref = await _seed_portfolio(session)
    account_id = await _account_id_for(session, business_id)
    account_ref = (
        await session.execute(
            text("SELECT account_ref FROM platform_accounts WHERE id = :id"),
            {"id": account_id},
        )
    ).scalar_one()
    port = SqlPanelReadPort(session, clock=FixedClock(_NOW))

    view = await port.get_portfolio(str(business_id), window="7D")

    row = next(row for row in view.rows if row.entity_ref == entity_ref)
    assert row.platform_account_id == account_ref
    assert AccountRef.parse(row.platform_account_id) == AccountRef.parse(account_ref)
    assert row.platform_account_id != str(account_id)
    assert row.platform_account_uuid == str(account_id)


async def test_a_never_ingested_sibling_account_never_masks_a_fresh_account(
    session: AsyncSession,
) -> None:
    """Hotfix 0.2.20 Bug B repro: the owner's business has fresh data (10 min
    old) on one account and a second, just-connected account with zero
    ingested facts. The aggregate must report the real 10 min lag, never the
    old maximally-stale sentinel from the empty sibling."""
    business_id, _entity_ref = await _seed_portfolio(session)
    other_account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO platform_accounts (id, business_id, platform, external_account_id, "
            "currency, timezone, api_tier, status) "
            "VALUES (:id, :business_id, 'meta', :external_account_id, 'EUR', "
            "'Europe/Madrid', 'meta_full', 'ACTIVE')"
        ),
        {
            "id": other_account_id,
            "business_id": business_id,
            "external_account_id": f"act_{other_account_id.hex[:12]}",
        },
    )
    await session.flush()
    port = SqlPanelReadPort(session, clock=FixedClock(_NOW))

    view = await port.get_portfolio(str(business_id), window="7D")
    freshness_items = await port.get_freshness(str(business_id))

    assert view.freshness.lag_minutes == 10
    assert view.freshness.is_stale is False
    assert view.freshness.no_data is False
    assert len(freshness_items) == 2
    assert {item.no_data for item in freshness_items} == {True, False}


async def test_account_without_any_ingested_fact_is_no_data_not_stale(
    session: AsyncSession,
) -> None:
    """Hotfix 0.2.20 Bug B: an account that never ingested anything is not
    "stale data" (which would show a fake ~16666h age and block writes) --
    it is the absence of data, reported as `no_data=True`/`is_stale=False`."""
    entity_ref_vo = campaign_ref(f"camp{uuid.uuid4().hex[:8]}", platform_value="meta")
    business_id = await seed_entity(session, entity_ref_vo)
    await session.flush()
    port = SqlPanelReadPort(session, clock=FixedClock(_NOW))

    freshness_items = await port.get_freshness(str(business_id))

    assert len(freshness_items) == 1
    assert freshness_items[0].is_stale is False
    assert freshness_items[0].no_data is True
    assert freshness_items[0].lag_minutes < 1_000_000


# ---------------------------------------------------------------------------
# `pending_proposals`/`deferred_proposals`/`ProposalBadges` (us2-sqlrepos):
# `_COUNT_PROPOSAL_BADGES` cuenta filas reales de `proposals`, no un doble
# en memoria -- sembradas directamente contra la tabla (mismo patron que
# `tests/integration/migrations/test_proposals.py::make_proposal`, pero via
# `AsyncSession` para compartir la conexion rolled-back del test en vez de
# abrir una `asyncpg.Connection` aparte).
# ---------------------------------------------------------------------------


def _diff_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


async def _insert_proposal(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    entity_ref: str,
    parameter: str,
    state: str,
    urgency: str,
    postponed_until: datetime | None = None,
) -> None:
    await session.execute(
        text("""
            INSERT INTO proposals
                (business_id, entity_ref, parameter, current_value, proposed_value, diff_hash,
                 classification, cause_key, cause, estimated_impact_amount,
                 estimated_impact_currency, urgency, state, postponed_until, postponed_reason,
                 expires_at)
            VALUES
                (:business_id, :entity_ref, :parameter, '{"amount": 60}'::jsonb,
                 '{"amount": 90}'::jsonb, :diff_hash, 'routine', 'limitada-por-presupuesto',
                 'Limitada por presupuesto', 100, 'EUR', :urgency, :state, :postponed_until,
                 :postponed_reason, now() + interval '24 hours')
        """),
        {
            "business_id": business_id,
            "entity_ref": entity_ref,
            "parameter": parameter,
            "diff_hash": _diff_hash(f"{entity_ref}|{parameter}|{state}"),
            "urgency": urgency,
            "state": state,
            "postponed_until": postponed_until,
            # 0023_owner_settings: `postponed_reason` es obligatorio en vivo
            # cuando `state = 'postponed'` (mismo criterio que ya exigia
            # `postponed_until`).
            "postponed_reason": "owner" if state == "postponed" else None,
        },
    )


async def _seed_proposal_badges(session: AsyncSession) -> tuple[uuid.UUID, str]:
    entity_ref_vo = campaign_ref(f"camp{uuid.uuid4().hex[:8]}", platform_value="google")
    business_id = await seed_entity(session, entity_ref_vo)
    entity_ref = str(entity_ref_vo)

    # 2 pending (1 critical, 1 recommended), 1 postponed (deferred), 1
    # rejected (resuelta -- no debe contar en ninguna insignia).
    await _insert_proposal(
        session,
        business_id=business_id,
        entity_ref=entity_ref,
        parameter="daily_budget",
        state="pending",
        urgency="critical",
    )
    await _insert_proposal(
        session,
        business_id=business_id,
        entity_ref=entity_ref,
        parameter="target_cpa",
        state="pending",
        urgency="recommended",
    )
    await _insert_proposal(
        session,
        business_id=business_id,
        entity_ref=entity_ref,
        parameter="bid_strategy",
        state="postponed",
        urgency="minor",
        postponed_until=_NOW + timedelta(hours=6),
    )
    await _insert_proposal(
        session,
        business_id=business_id,
        entity_ref=entity_ref,
        parameter="status",
        state="rejected",
        urgency="recommended",
    )
    await session.flush()
    return business_id, entity_ref


async def test_get_portfolio_proposal_counts_match_hand_computed_expectations(
    session: AsyncSession,
) -> None:
    business_id, _entity_ref = await _seed_proposal_badges(session)
    port = SqlPanelReadPort(session, clock=FixedClock(_NOW))

    view = await port.get_portfolio(str(business_id), window="7D")

    assert view.pending_proposals == 2  # critical + recommended, ambas 'pending'
    assert view.deferred_proposals == 1  # solo la 'postponed'


async def test_get_badges_proposal_counts_match_hand_computed_expectations(
    session: AsyncSession,
) -> None:
    business_id, _entity_ref = await _seed_proposal_badges(session)
    port = SqlPanelReadPort(session, clock=FixedClock(_NOW))

    badges = await port.get_badges(str(business_id), signals_since=None)

    assert badges.proposals.pending == 2
    assert badges.proposals.critical == 1  # solo la 'pending' con urgency='critical'
    assert badges.proposals.deferred == 1


async def test_list_signals_with_no_filters_does_not_raise_ambiguous_parameter(
    session: AsyncSession,
) -> None:
    """Regresion: `kind`/`min_strength`/`since`/`entity_ref` en `None`
    lanzaba `asyncpg.exceptions.AmbiguousParameterError` porque `:x IS
    NULL` sin `CAST` deja a asyncpg sin tipo con el que preparar el
    parametro (mismo patron ya corregido en `mcp/infrastructure/
    sql_signal_read_port.py`)."""
    entity_ref = campaign_ref(f"nul{uuid.uuid4().hex[:8]}", platform_value="google")
    business_id = await seed_entity(session, entity_ref)
    await session.execute(
        _INSERT_SIGNAL,
        {
            "business_id": business_id,
            "entity_ref": str(entity_ref),
            "kind": "SELL",
            "window_start": _NOW.date() - timedelta(days=6),
            "window_end": _NOW.date(),
            "cycle_id": uuid.uuid4(),
            "emitted_at": _NOW - timedelta(days=2),
        },
    )
    await session.flush()
    port = SqlPanelReadPort(session, clock=FixedClock(_NOW))

    page = await port.list_signals(
        str(business_id),
        kind=None,
        min_strength=None,
        since=None,
        platform=None,
        entity_ref=None,
        limit=None,
        cursor=None,
    )

    assert any(item.entity_ref == str(entity_ref) for item in page.items)
    assert next(item for item in page.items if item.entity_ref == str(entity_ref)).kind == "SELL"

    # The same stored signal is embedded in /portfolio: both responses must
    # preserve the uppercase DB enums consumed by the panel's Zod schemas.
    view = await port.get_portfolio(str(business_id), window="7D")
    row = next(item for item in view.rows if item.entity_ref == str(entity_ref))
    assert row.status == "ACTIVE"
    assert row.signal is not None
    assert row.signal.kind == "SELL"


async def _seed_child(session: AsyncSession, parent_ref: str, level: str) -> str:
    return (
        await session.execute(
            text("""
        INSERT INTO ad_entities
            (business_id, platform_account_id, platform, level, external_id, name,
             status, platform_state_hash, parent_id, parent_level)
        SELECT business_id, platform_account_id, platform, :level, :external_id,
               'Child without invented metrics', 'PAUSED', platform_state_hash, id, level
          FROM ad_entities WHERE entity_ref=:parent_ref
        RETURNING entity_ref
    """),
            {"level": level, "external_id": uuid.uuid4().hex, "parent_ref": parent_ref},
        )
    ).scalar_one()


@pytest.mark.parametrize("platform", ["google", "meta"])
async def test_children_real_metrics_null_absence_and_hierarchy(
    session: AsyncSession,
    platform: str,
) -> None:
    parent = campaign_ref(uuid.uuid4().hex, platform_value=platform)
    business = await seed_entity(session, parent)
    child_ref = await _seed_child(session, str(parent), "ad_set")
    leaf_ref = await _seed_child(session, child_ref, "ad")
    other_parent = campaign_ref(uuid.uuid4().hex, platform_value=platform)
    await seed_entity(session, other_parent)
    await _seed_child(session, str(other_parent), "ad_set")
    port = SqlPanelReadPort(session, FixedClock(_NOW))
    children = await port.get_entity_children(str(parent))
    assert len(children) == 1
    child = children[0]
    assert child.entity_ref == child_ref
    assert child.status == "PAUSED"
    assert child.has_children is True
    assert child.spend_today is None
    assert child.spend_window is None
    assert child.conversions_by_kind is None
    assert child.freshness is None
    leaves = await port.get_entity_children(child_ref)
    assert [item.entity_ref for item in leaves] == [leaf_ref]
    assert leaves[0].has_children is False
    assert await port.get_entity_children(leaf_ref) == []

    await session.execute(
        text("""
        INSERT INTO metrics_daily
            (business_id, entity_ref, entity_level, platform_account_id, stat_date,
             account_timezone, currency, spend, impressions, clicks, conversions_lead,
             conversion_value, ingested_at)
        SELECT business_id, entity_ref, level, platform_account_id, :day,
               'Europe/Madrid', 'EUR', :spend, 100, 10, 2, 0, :ingested
          FROM ad_entities WHERE entity_ref=:ref
    """),
        {"day": _TODAY, "spend": 5000, "ingested": _NOW - timedelta(minutes=5), "ref": child_ref},
    )
    await session.execute(
        _INSERT_SIGNAL,
        {
            "business_id": business,
            "entity_ref": child_ref,
            "kind": "SELL",
            "window_start": _TODAY - timedelta(days=6),
            "window_end": _TODAY,
            "cycle_id": uuid.uuid4(),
            "emitted_at": _NOW,
        },
    )
    child = (await port.get_entity_children(str(parent)))[0]
    assert child.spend_today is not None and child.spend_today.amount == Decimal("50")
    assert child.spend_window is not None and child.spend_window.amount == Decimal("50")
    assert child.cost_per_lead is not None and child.cost_per_lead.amount == Decimal("25")
    assert child.conversions_by_kind == {
        "lead": 2,
        "whatsapp": 0,
        "call": 0,
        "business_conversion": 0,
    }
    assert child.freshness is not None and child.freshness.lag_minutes == 5
    assert child.signal is not None and child.signal.kind == "SELL"
