"""`SqlCockpitReadModel` (026, tasks.md T007/T012) contra Postgres real:
reglas de honestidad de cockpit-read-model.md §4, orden por `money_at_stake`,
paridad campo a campo con `GET /portfolio` (SC-003), `no_customer_source`
sin puente CRM configurado, valores reales de clientes/ROI con puente
configurado y datos (spec 027 T017), y la correlacion señal->propuesta que
sostiene `RowAction` (T010)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.crm.domain.bridge_health import ConnectorBridgeState, CrmBridgeHealth
from safent_ads.crm.domain.customer import Customer
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import AttributionRung
from safent_ads.crm.domain.revenue_event import RevenueEvent, RevenueEventId, RevenueEventKind
from safent_ads.crm.infrastructure.sql_crm_bridge_health_repository import (
    SqlCrmBridgeHealthRepository,
)
from safent_ads.crm.infrastructure.sql_customer_repository import SqlCustomerRepository
from safent_ads.crm.infrastructure.sql_revenue_event_repository import SqlRevenueEventRepository
from safent_ads.panel.application.cockpit_dto import ActionKind, ActionMode
from safent_ads.panel.infrastructure.sql_cockpit_read_model import SqlCockpitReadModel
from safent_ads.panel.infrastructure.sql_read_model import SqlPanelReadPort
from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.proposals.domain.money import Money as ProposalMoney
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.rules.infrastructure.sql_repositories import SqlGuardrailRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.shared.read_models.dto import MeasureStatus
from tests.conftest import rolled_back_session
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
_TODAY = date(2026, 3, 15)

_INSERT_SIGNAL = text("""
    INSERT INTO signals (id, business_id, entity_ref, kind, strength, cause, cause_code,
                         rule_code, gate_verdicts, evidence, data_window, window_start,
                         window_end, money_at_stake_minor, money_at_stake_currency,
                         cycle_id, emitted_at)
    VALUES (:id, :business_id, :entity_ref, :kind, 78, 'CPL 41 vs 28', 'CPL_OVER_TARGET', 'M05',
            '[]'::jsonb, '{}'::jsonb,
            '7D', :window_start, :window_end, :money_at_stake_minor, 'EUR', :cycle_id,
            :emitted_at)
""")

_INSERT_DAILY_FACT = text("""
    INSERT INTO metrics_daily
        (business_id, entity_ref, entity_level, platform_account_id, stat_date,
         account_timezone, currency, spend, impressions, clicks, conversions_lead,
         conversion_value, ingested_at)
    VALUES
        (:business_id, :entity_ref, 'campaign', :account_id, :stat_date,
         'Europe/Madrid', 'EUR', :spend, 100, 10, :conversions_lead, 0, :ingested_at)
""")


def _money_amount(minor: int) -> Decimal:
    return Decimal(minor) / 100


def _money_value(minor: int) -> str:
    return json.dumps({"type": "money", "amount": str(_money_amount(minor)), "currency": "EUR"})


def _evidence_envelope(*, signal_id: uuid.UUID, rule_id: str = "M05") -> str:
    return json.dumps(
        {
            "items": [],
            "cause": {"signal_id": str(signal_id), "rule_id": rule_id},
            "cause_key": {"rule_id": rule_id, "cause_type": "cpl_over_target"},
            "expected_state_hash": None,
        }
    )


async def _account_id_for(session: AsyncSession, business_id: uuid.UUID) -> uuid.UUID:
    result = await session.execute(
        text("SELECT id FROM platform_accounts WHERE business_id = :business_id"),
        {"business_id": business_id},
    )
    return result.scalar_one()


async def _seed_fresh_entity(
    session: AsyncSession, *, platform: str = "google", spend_minor: int = 5_000
) -> tuple[uuid.UUID, str]:
    entity_ref_vo = campaign_ref(f"camp{uuid.uuid4().hex[:8]}", platform_value=platform)
    business_id = await seed_entity(session, entity_ref_vo)
    account_id = await _account_id_for(session, business_id)
    entity_ref = str(entity_ref_vo)
    await session.execute(
        _INSERT_DAILY_FACT,
        {
            "business_id": business_id,
            "entity_ref": entity_ref,
            "account_id": account_id,
            "stat_date": _TODAY,
            "spend": spend_minor,
            "conversions_lead": 2,
            "ingested_at": _NOW - timedelta(minutes=10),
        },
    )
    await SqlGuardrailRepository(session).save_for_account(
        account_ref=f"{platform}:{account_external_id(entity_ref_vo)}",
        policy=GuardrailPolicy(
            daily_cap_minor=10_000,
            monthly_cap_minor=100_000,
            floor_minor=0,
            ceiling_minor=100_000,
            max_step_pct=20.0,
            max_changes_per_day=5,
        ),
        currency="EUR",
    )
    await session.flush()
    return business_id, entity_ref


async def _insert_signal(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    entity_ref: str,
    kind: str,
    money_at_stake_minor: int,
) -> uuid.UUID:
    signal_id = uuid.uuid4()
    await session.execute(
        _INSERT_SIGNAL,
        {
            "id": signal_id,
            "business_id": business_id,
            "entity_ref": entity_ref,
            "kind": kind,
            "window_start": _NOW.date() - timedelta(days=6),
            "window_end": _NOW.date(),
            "money_at_stake_minor": money_at_stake_minor,
            "cycle_id": uuid.uuid4(),
            "emitted_at": _NOW - timedelta(hours=1),
        },
    )
    await session.flush()
    return signal_id


_PROPOSAL_PARAMETER = "daily_budget_minor"


async def _insert_pending_proposal(
    session: AsyncSession, *, business_id: uuid.UUID, entity_ref: str, signal_id: uuid.UUID
) -> uuid.UUID:
    proposal_id = uuid.uuid4()
    before = ProposalMoney(_money_amount(6_000), "EUR")
    after = ProposalMoney(_money_amount(9_000), "EUR")
    diff_hash = compute_diff_hash(EntityRef.parse(entity_ref), _PROPOSAL_PARAMETER, before, after)
    await session.execute(
        text("""
            INSERT INTO proposals
                (id, business_id, entity_ref, parameter, current_value, proposed_value,
                 diff_hash, classification, cause_key, cause, evidence,
                 estimated_impact_amount, estimated_impact_currency, urgency, signal_id,
                 state, expires_at)
            VALUES
                (:id, :business_id, :entity_ref, :parameter,
                 CAST(:current_value AS JSONB), CAST(:proposed_value AS JSONB), :diff_hash,
                 'routine', 'cpl_over_target', 'CPL 41 vs 28 objetivo',
                 CAST(:evidence AS JSONB), 50, 'EUR', 'recommended', :signal_id, 'pending',
                 now() + interval '24 hours')
        """),
        {
            "id": proposal_id,
            "business_id": business_id,
            "entity_ref": entity_ref,
            "parameter": _PROPOSAL_PARAMETER,
            "current_value": _money_value(6_000),
            "proposed_value": _money_value(9_000),
            "diff_hash": diff_hash,
            "evidence": _evidence_envelope(signal_id=signal_id),
            "signal_id": signal_id,
        },
    )
    await session.flush()
    return proposal_id


async def _seed_second_entity_in_same_business(
    session: AsyncSession, *, business_id: uuid.UUID, platform: str = "google"
) -> str:
    """Segunda entidad bajo el MISMO negocio (`seed_entity` siempre crea uno
    nuevo): reusa la cuenta ya sembrada, solo inserta `ad_entities`."""
    account_id = await _account_id_for(session, business_id)
    entity_ref_vo = campaign_ref(f"camp{uuid.uuid4().hex[:8]}", platform_value=platform)
    await session.execute(
        text("""
            INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                     external_id, name, status, platform_state_hash)
            VALUES (:business_id, :account_id, :platform, :level, :external_id,
                    'Segunda campana de contrato', 'ACTIVE', :state_hash)
        """),
        {
            "business_id": business_id,
            "account_id": account_id,
            "platform": entity_ref_vo.platform.value,
            "level": entity_ref_vo.level.value,
            "external_id": entity_ref_vo.external_id,
            "state_hash": "b" * 64,
        },
    )
    await session.flush()
    return str(entity_ref_vo)


@pytest.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    async with rolled_back_session(database_url) as open_session:
        yield open_session


async def test_get_cockpit_matches_portfolio_on_spend_and_caps(session: AsyncSession) -> None:
    business_id, _entity_ref = await _seed_fresh_entity(session)
    cockpit = SqlCockpitReadModel(session, clock=FixedClock(_NOW))
    panel = SqlPanelReadPort(session, clock=FixedClock(_NOW))

    view = await cockpit.get_cockpit(str(business_id), window="7d")
    portfolio = await panel.get_portfolio(str(business_id), window="7D")

    assert view.header.spend.today == portfolio.spend.today
    assert view.header.spend.mtd == portfolio.spend.mtd
    assert view.header.caps.monthly == portfolio.caps.monthly
    assert view.is_partial is False
    assert len(view.rows) == len(portfolio.rows) == 1
    assert view.rows[0].spend == portfolio.rows[0].spend
    assert view.rows[0].money_at_stake == portfolio.rows[0].money_at_stake


async def test_rows_are_ordered_by_money_at_stake_descending(session: AsyncSession) -> None:
    business_id, entity_a = await _seed_fresh_entity(session, spend_minor=1_000)
    entity_b = await _seed_second_entity_in_same_business(session, business_id=business_id)
    await _insert_signal(
        session, business_id=business_id, entity_ref=entity_a, kind="SELL", money_at_stake_minor=500
    )
    await _insert_signal(
        session,
        business_id=business_id,
        entity_ref=entity_b,
        kind="SELL",
        money_at_stake_minor=9_000,
    )
    cockpit = SqlCockpitReadModel(session, clock=FixedClock(_NOW))

    view = await cockpit.get_cockpit(str(business_id), window="7d")

    stakes = [row.money_at_stake.amount for row in view.rows]
    assert stakes == sorted(stakes, reverse=True)
    assert view.rows[0].entity_ref == entity_b


async def test_not_controllable_entity_blocks_the_row_action(session: AsyncSession) -> None:
    business_id, entity_ref = await _seed_fresh_entity(session)
    await session.execute(
        text("UPDATE ad_entities SET is_controllable = false WHERE business_id = :business_id"),
        {"business_id": business_id},
    )
    await _insert_signal(
        session,
        business_id=business_id,
        entity_ref=entity_ref,
        kind="BUY",
        money_at_stake_minor=1_000,
    )
    cockpit = SqlCockpitReadModel(session, clock=FixedClock(_NOW))

    view = await cockpit.get_cockpit(str(business_id), window="7d")

    row = view.rows[0]
    assert row.is_controllable is False
    assert row.action.kind is ActionKind.NONE
    assert row.action.mode is ActionMode.BLOCKED
    assert row.action.blocked_reason is not None
    assert row.action.blocked_reason.value == "not_controllable"


async def test_never_ingested_freshness_is_no_data_and_does_not_block_rows(
    session: AsyncSession,
) -> None:
    """Hotfix 0.2.20 Bug B: a business whose only account never ingested
    anything is `no_data`, not `is_stale` -- it must not block every row
    the way genuinely stale (>60 min old) data does."""
    entity_ref_vo = campaign_ref(f"camp{uuid.uuid4().hex[:8]}", platform_value="google")
    business_id = await seed_entity(entity_ref=entity_ref_vo, session=session)
    await session.flush()
    cockpit = SqlCockpitReadModel(session, clock=FixedClock(_NOW))

    view = await cockpit.get_cockpit(str(business_id), window="7d")

    assert view.freshness.is_stale is False
    assert view.freshness.no_data is True


async def test_genuinely_stale_freshness_still_blocks_every_row(session: AsyncSession) -> None:
    entity_ref_vo = campaign_ref(f"camp{uuid.uuid4().hex[:8]}", platform_value="google")
    business_id = await seed_entity(entity_ref=entity_ref_vo, session=session)
    account_id = await _account_id_for(session, business_id)
    await session.execute(
        _INSERT_DAILY_FACT,
        {
            "business_id": business_id,
            "entity_ref": str(entity_ref_vo),
            "account_id": account_id,
            "stat_date": _TODAY,
            "spend": 1_000,
            "conversions_lead": 1,
            "ingested_at": _NOW - timedelta(hours=2),
        },
    )
    await session.flush()
    cockpit = SqlCockpitReadModel(session, clock=FixedClock(_NOW))

    view = await cockpit.get_cockpit(str(business_id), window="7d")

    assert view.freshness.is_stale is True
    assert view.freshness.no_data is False


async def test_no_guardrail_seeded_is_partial_and_projected_month_end_unavailable(
    session: AsyncSession,
) -> None:
    entity_ref_vo = campaign_ref(f"camp{uuid.uuid4().hex[:8]}", platform_value="google")
    business_id = await seed_entity(session, entity_ref_vo)
    account_id = await _account_id_for(session, business_id)
    await session.execute(
        _INSERT_DAILY_FACT,
        {
            "business_id": business_id,
            "entity_ref": str(entity_ref_vo),
            "account_id": account_id,
            "stat_date": _TODAY,
            "spend": 1_000,
            "conversions_lead": 1,
            "ingested_at": _NOW - timedelta(minutes=5),
        },
    )
    await session.flush()
    cockpit = SqlCockpitReadModel(session, clock=FixedClock(_NOW))

    view = await cockpit.get_cockpit(str(business_id), window="7d")

    assert view.is_partial is True
    assert view.header.projected_month_end.status is MeasureStatus.NO_DATA
    assert view.rows[0].pacing_index_pct.status is MeasureStatus.NO_DATA


async def test_customers_and_roi_are_no_customer_source_without_a_configured_bridge(
    session: AsyncSession,
) -> None:
    business_id, _entity_ref = await _seed_fresh_entity(session)
    cockpit = SqlCockpitReadModel(session, clock=FixedClock(_NOW))

    view = await cockpit.get_cockpit(str(business_id), window="7d")

    assert view.header.customers.today.status is MeasureStatus.NO_CUSTOMER_SOURCE
    assert view.header.customers.week.status is MeasureStatus.NO_CUSTOMER_SOURCE
    assert view.header.roi.status is MeasureStatus.NO_CUSTOMER_SOURCE
    assert view.rows[0].customers.status is MeasureStatus.NO_CUSTOMER_SOURCE
    assert view.rows[0].customer_value.status is MeasureStatus.NO_CUSTOMER_SOURCE


async def test_buy_signal_with_a_pending_proposal_resolves_to_approve_increase(
    session: AsyncSession,
) -> None:
    business_id, entity_ref = await _seed_fresh_entity(session)
    signal_id = await _insert_signal(
        session,
        business_id=business_id,
        entity_ref=entity_ref,
        kind="BUY",
        money_at_stake_minor=3_000,
    )
    proposal_id = await _insert_pending_proposal(
        session, business_id=business_id, entity_ref=entity_ref, signal_id=signal_id
    )
    cockpit = SqlCockpitReadModel(session, clock=FixedClock(_NOW))

    view = await cockpit.get_cockpit(str(business_id), window="7d")

    row = view.rows[0]
    assert row.signal is not None
    assert row.signal.signal_id == str(signal_id)
    assert row.action.kind is ActionKind.APPROVE_INCREASE
    assert row.action.mode is ActionMode.INLINE_APPROVAL
    assert row.action.proposal_id == str(proposal_id)
    assert row.action.target is not None
    assert row.action.target.path == f"/api/v1/proposals/{proposal_id}/approve"


async def test_get_changes_since_lists_signal_changes_and_declares_itself_partial(
    session: AsyncSession,
) -> None:
    business_id, entity_ref = await _seed_fresh_entity(session)
    await _insert_signal(
        session,
        business_id=business_id,
        entity_ref=entity_ref,
        kind="SELL",
        money_at_stake_minor=2_000,
    )
    cockpit = SqlCockpitReadModel(session, clock=FixedClock(_NOW))

    strip = await cockpit.get_changes_since(str(business_id), since=_NOW - timedelta(days=1))

    assert strip.is_partial is True
    assert any(item.entity_ref == entity_ref and item.after == "SELL" for item in strip.items)


async def test_configured_bridge_without_customers_yet_is_no_data_not_no_source(
    session: AsyncSession,
) -> None:
    business_id, _entity_ref = await _seed_fresh_entity(session)
    await SqlCrmBridgeHealthRepository(session).upsert(
        CrmBridgeHealth.evaluate(
            business_id=BusinessId(business_id),
            connector_id="connector-crm",
            connector_state=ConnectorBridgeState.READY,
            last_event_at=_NOW,
            as_of=_NOW,
            cause=None,
        )
    )
    await session.flush()
    cockpit = SqlCockpitReadModel(session, clock=FixedClock(_NOW))

    view = await cockpit.get_cockpit(str(business_id), window="7d")

    assert view.header.customers.today.status is MeasureStatus.NO_DATA
    assert view.header.roi.status is MeasureStatus.NO_DATA
    assert view.rows[0].customers.status is MeasureStatus.NO_DATA


async def test_configured_bridge_with_customers_reports_real_values(
    session: AsyncSession,
) -> None:
    business_id, entity_ref = await _seed_fresh_entity(session, spend_minor=5_000)
    typed_business_id = BusinessId(business_id)
    await SqlCrmBridgeHealthRepository(session).upsert(
        CrmBridgeHealth.evaluate(
            business_id=typed_business_id,
            connector_id="connector-crm",
            connector_state=ConnectorBridgeState.READY,
            last_event_at=_NOW,
            as_of=_NOW,
            cause=None,
        )
    )
    identity = HashedIdentity.compute(
        business_id=typed_business_id, raw_identifier="a@example.com", salt="s"
    )
    customer = Customer.first_seen(
        business_id=typed_business_id,
        hashed_identity=identity,
        entity_ref=EntityRef.parse(entity_ref),
        attribution_rung=AttributionRung.HASHED_IDENTITY,
        currency="EUR",
        seen_at=_NOW - timedelta(days=1),
    ).record_paid_event(occurred_at=_NOW - timedelta(days=1))
    customers = SqlCustomerRepository(session)
    await customers.upsert(customer)
    await SqlRevenueEventRepository(session).insert_if_new(
        RevenueEvent(
            revenue_event_id=RevenueEventId.new(),
            customer_id=customer.customer_id,
            kind=RevenueEventKind.FIRST_PAYMENT,
            amount_minor=10_000,
            currency="EUR",
            occurred_at=_NOW - timedelta(days=1),
            observed_at=_NOW - timedelta(days=1),
            source_event_id="evt-1",
            mapping_version=1,
        ),
        business_id=typed_business_id,
        connector_id="connector-crm",
    )
    await session.flush()
    cockpit = SqlCockpitReadModel(session, clock=FixedClock(_NOW))

    view = await cockpit.get_cockpit(str(business_id), window="7d")

    assert view.header.customers.week.status is MeasureStatus.AVAILABLE
    assert view.header.customers.week.value == 1
    assert view.header.roi.status is MeasureStatus.AVAILABLE
    row = view.rows[0]
    assert row.customers.status is MeasureStatus.AVAILABLE
    assert row.customers.value == 1
    assert row.customer_value.status is MeasureStatus.AVAILABLE
    assert row.customer_value.value is not None
    assert row.customer_value.value.amount == Decimal("100")
    assert row.roi.status is MeasureStatus.AVAILABLE
