"""`SqlBudgetEnvelopeReadPort` contra Postgres real (P2, tool-surface.md
§2.1): igual que `test_portfolio_read_port_contract.py`, este puerto abre
su PROPIA sesion por llamada, asi que necesita datos COMMITEADOS -- se
siembra con `seed_entity`/`SqlGuardrailRepository` y se compara contra
cifras calculadas a mano, nunca contra un doble en memoria."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity

from safent_ads.mcp.infrastructure.sql_budget_envelope_read_port import SqlBudgetEnvelopeReadPort
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.rules.infrastructure.sql_repositories import SqlGuardrailRepository
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration

_TODAY = date(2026, 3, 15)  # dia 15 de marzo (31 dias): ritmo facil de comprobar a mano
_NOW = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
_SPEND_MINOR = 15_500
_MONTHLY_CAP_MINOR = 62_000


async def _insert_daily_fact(
    session: AsyncSession, *, business_id: uuid.UUID, entity_ref: str, spend_minor: int
) -> None:
    account_id = (
        await session.execute(
            text("SELECT id FROM platform_accounts WHERE business_id = :business_id"),
            {"business_id": business_id},
        )
    ).scalar_one()
    await session.execute(
        text("""
            INSERT INTO metrics_daily
                (business_id, entity_ref, entity_level, platform_account_id, stat_date,
                 account_timezone, currency, spend, impressions, clicks, conversions_lead,
                 conversion_value, ingested_at)
            VALUES
                (:business_id, :entity_ref, 'campaign', :account_id, :stat_date,
                 'Europe/Madrid', 'EUR', :spend, 100, 10, 2, 0, :ingested_at)
        """),
        {
            "business_id": business_id,
            "entity_ref": entity_ref,
            "account_id": account_id,
            "stat_date": _TODAY,
            "spend": spend_minor,
            "ingested_at": _NOW - timedelta(minutes=10),
        },
    )


async def _seed_account_with_spend(
    factory: async_sessionmaker[AsyncSession], *, spend_minor: int = _SPEND_MINOR
) -> uuid.UUID:
    entity_ref = campaign_ref(f"be{uuid.uuid4().hex[:8]}", platform_value="google")
    async with factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await _insert_daily_fact(
            session, business_id=business_id, entity_ref=str(entity_ref), spend_minor=spend_minor
        )
        await session.commit()
    return business_id


async def _seed_guardrail(
    factory: async_sessionmaker[AsyncSession], *, account_ref: str, monthly_cap_minor: int
) -> None:
    async with factory() as session:
        await SqlGuardrailRepository(session).save_for_account(
            account_ref=account_ref,
            policy=GuardrailPolicy(
                daily_cap_minor=monthly_cap_minor // 30,
                monthly_cap_minor=monthly_cap_minor,
                floor_minor=0,
                ceiling_minor=monthly_cap_minor,
                max_step_pct=20.0,
                max_changes_per_day=5,
            ),
            currency="EUR",
        )
        await session.commit()


async def test_no_platform_account_reports_null_fields_with_a_reason(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    port = SqlBudgetEnvelopeReadPort(mcp_session_factory, FixedClock(_NOW))

    envelope = await port.get_budget_envelope(str(uuid.uuid4()))

    assert envelope.monthly_cap_minor is None
    assert envelope.spent_month_to_date_minor is None
    assert envelope.headroom_minor is None
    assert envelope.projected_month_end_minor is None
    assert envelope.currency is None
    assert envelope.reason == "no_platform_account"
    assert envelope.as_of == _TODAY


async def test_account_without_a_guardrail_still_reports_real_spend_and_projection(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    business_id = await _seed_account_with_spend(mcp_session_factory)
    port = SqlBudgetEnvelopeReadPort(mcp_session_factory, FixedClock(_NOW))

    envelope = await port.get_budget_envelope(str(business_id))

    assert envelope.monthly_cap_minor is None
    assert envelope.headroom_minor is None
    assert envelope.reason == "no_caps_entry"
    assert envelope.currency == "EUR"
    assert envelope.spent_month_to_date_minor == _SPEND_MINOR
    # 15.500 en 15 dias de un mes de 31 -> proyeccion lineal a 31 dias.
    assert envelope.projected_month_end_minor == round(_SPEND_MINOR / 15 * 31)


async def test_account_with_a_known_cap_computes_headroom_from_real_spend(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    entity_ref = campaign_ref(f"be{uuid.uuid4().hex[:8]}", platform_value="google")
    async with mcp_session_factory() as session:
        business_id = await seed_entity(session, entity_ref)
        await _insert_daily_fact(
            session, business_id=business_id, entity_ref=str(entity_ref), spend_minor=_SPEND_MINOR
        )
        await session.commit()
    account_ref = f"google:{account_external_id(entity_ref)}"
    await _seed_guardrail(
        mcp_session_factory, account_ref=account_ref, monthly_cap_minor=_MONTHLY_CAP_MINOR
    )
    port = SqlBudgetEnvelopeReadPort(mcp_session_factory, FixedClock(_NOW))

    envelope = await port.get_budget_envelope(str(business_id))

    assert envelope.reason is None
    assert envelope.monthly_cap_minor == _MONTHLY_CAP_MINOR
    assert envelope.spent_month_to_date_minor == _SPEND_MINOR
    assert envelope.headroom_minor == _MONTHLY_CAP_MINOR - _SPEND_MINOR
    assert envelope.projected_month_end_minor == round(_SPEND_MINOR / 15 * 31)
    assert envelope.currency == "EUR"
