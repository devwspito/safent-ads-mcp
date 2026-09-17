"""`SqlCrmSummaryReadPort` (R2, historia 12): unica lectura de CRM del MCP,
agregada y sin PII (D-6, threat-model.md §1). El minimo de agregacion k=5
se comprueba contra Postgres de verdad -- un cubo con menos de 5 clientes
nunca sale con su valor real, sin importar si es cero o cuatro."""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp.application.company_read_ports import (
    ChannelRevenue,
    CrmSummary,
    CrmWindowPreset,
)
from safent_ads.mcp.infrastructure.sql_crm_read_port import SqlCrmSummaryReadPort
from safent_ads.shared.clock import FixedClock

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
_OUTSIDE_WINDOW = _NOW - timedelta(days=40)
_PAID_AMOUNT_MINOR = 10_000
_SUSPICIOUS_OUTSIDE_AMOUNT_MINOR = 999_999


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


async def _seed_business(session: AsyncSession) -> uuid.UUID:
    business_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO businesses (id, slug, name, timezone, reference_currency)
            VALUES (:id, :slug, 'CRM contrato', 'Europe/Madrid', 'EUR')
            """
        ),
        {"id": business_id, "slug": f"crm-{business_id.hex[:12]}"},
    )
    return business_id


async def _seed_campaign_entity(session: AsyncSession, business_id: uuid.UUID) -> str:
    credential_id = uuid.uuid4()
    account_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:12]
    await session.execute(
        text("INSERT INTO credential_refs (id, platform, alias) VALUES (:id, 'meta', :alias)"),
        {"id": credential_id, "alias": f"alias-{suffix}"},
    )
    await session.execute(
        text(
            """
            INSERT INTO platform_accounts (id, business_id, platform, external_account_id,
                                            currency, timezone, api_tier, credential_ref_id,
                                            status)
            VALUES (:id, :business_id, 'meta', :external_account_id, 'EUR', 'Europe/Madrid',
                    'meta_full', :credential_ref_id, 'ACTIVE')
            """
        ),
        {
            "id": account_id,
            "business_id": business_id,
            "external_account_id": f"act_{suffix}",
            "credential_ref_id": credential_id,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                     external_id, name, status, platform_state_hash)
            VALUES (:business_id, :account_id, 'meta', 'campaign', :external_id,
                    'Campana CRM', 'ACTIVE', :state_hash)
            """
        ),
        {
            "business_id": business_id,
            "account_id": account_id,
            "external_id": suffix,
            "state_hash": "a" * 64,
        },
    )
    return f"meta:campaign:{suffix}"


async def _seed_customer(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    entity_ref: str | None,
    first_paid_conversion_at: datetime | None,
) -> uuid.UUID:
    customer_id = uuid.uuid4()
    seen_at = first_paid_conversion_at or _NOW
    await session.execute(
        text(
            """
            INSERT INTO customers (id, business_id, identity_digest, salt_version, entity_ref,
                                    attribution_rung, first_paid_conversion_at, state, currency,
                                    first_seen_at, last_seen_at)
            VALUES (:id, :business_id, :digest, 1, :entity_ref, 'hashed_identity',
                    :first_paid, 'active', 'EUR', :seen_at, :seen_at)
            """
        ),
        {
            "id": customer_id,
            "business_id": business_id,
            "digest": _digest(str(customer_id)),
            "entity_ref": entity_ref,
            "first_paid": first_paid_conversion_at,
            "seen_at": seen_at,
        },
    )
    return customer_id


async def _seed_revenue_event(
    session: AsyncSession,
    business_id: uuid.UUID,
    customer_id: uuid.UUID,
    *,
    kind: str,
    amount_minor: int,
    occurred_at: datetime,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO revenue_events (business_id, connector_id, customer_id, source_event_id,
                                         kind, amount_minor, currency, occurred_at, observed_at,
                                         mapping_version)
            VALUES (:business_id, 'stripe', :customer_id, :source_event_id, :kind, :amount_minor,
                    'EUR', :occurred_at, :occurred_at, 1)
            """
        ),
        {
            "business_id": business_id,
            "customer_id": customer_id,
            "source_event_id": f"evt-{uuid.uuid4().hex}",
            "kind": kind,
            "amount_minor": amount_minor,
            "occurred_at": occurred_at,
        },
    )


async def test_resumen_no_contiene_ningun_campo_identificable_ni_cubos_menores_de_cinco(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with mcp_session_factory() as session:
        business_id = await _seed_business(session)
        entity_ref = await _seed_campaign_entity(session, business_id)

        customer_ids = [
            await _seed_customer(
                session,
                business_id,
                entity_ref=entity_ref,
                first_paid_conversion_at=_NOW if index < 3 else None,
            )
            for index in range(6)
        ]
        for customer_id in customer_ids:
            await _seed_revenue_event(
                session,
                business_id,
                customer_id,
                kind="first_payment",
                amount_minor=_PAID_AMOUNT_MINOR,
                occurred_at=_NOW,
            )
        for customer_id in customer_ids[:2]:
            await _seed_revenue_event(
                session,
                business_id,
                customer_id,
                kind="recurring_payment",
                amount_minor=_PAID_AMOUNT_MINOR,
                occurred_at=_NOW,
            )

        # Ruido deliberado fuera de ventana: si el filtro de fecha fallara,
        # este importe absurdo lo delataria en `total_revenue_minor`.
        outside_customer = await _seed_customer(
            session, business_id, entity_ref=entity_ref, first_paid_conversion_at=_OUTSIDE_WINDOW
        )
        await _seed_revenue_event(
            session,
            business_id,
            outside_customer,
            kind="first_payment",
            amount_minor=_SUSPICIOUS_OUTSIDE_AMOUNT_MINOR,
            occurred_at=_OUTSIDE_WINDOW,
        )

        # Ruido de otro negocio: nunca debe filtrarse a este resumen.
        other_business_id = await _seed_business(session)
        other_customer = await _seed_customer(
            session, other_business_id, entity_ref=None, first_paid_conversion_at=_NOW
        )
        await _seed_revenue_event(
            session,
            other_business_id,
            other_customer,
            kind="first_payment",
            amount_minor=_SUSPICIOUS_OUTSIDE_AMOUNT_MINOR,
            occurred_at=_NOW,
        )
        await session.commit()

    port = SqlCrmSummaryReadPort(mcp_session_factory, FixedClock(_NOW))

    summary = await port.get_crm_summary(str(business_id), window=CrmWindowPreset.THIRTY_DAYS)

    # Cubos con menos de 5 clientes: null + nota, nunca el dato.
    assert summary.new_customers is None
    assert summary.new_customers_note == "cubo_con_menos_de_5_clientes"
    assert summary.returning_customers is None
    assert summary.returning_customers_note == "cubo_con_menos_de_5_clientes"

    # 6 clientes distintos (>=5): el cubo de ingresos si sale.
    assert summary.total_revenue_minor == 8 * _PAID_AMOUNT_MINOR
    assert summary.average_order_value_minor == _PAID_AMOUNT_MINOR
    assert summary.lifetime_value_estimate_minor == round(8 * _PAID_AMOUNT_MINOR / 6)
    assert summary.revenue_note is None

    assert summary.revenue_by_channel == (
        ChannelRevenue(channel="meta", revenue_minor=8 * _PAID_AMOUNT_MINOR, note=None),
    )

    field_names = {f.name for f in dataclasses.fields(CrmSummary)}
    forbidden_terms = ("email", "phone", "name", "customer_id", "hashed_identity")
    assert not any(term in field for field in field_names for term in forbidden_terms)


async def test_resumen_ignora_clientes_e_ingresos_de_otro_negocio(
    mcp_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with mcp_session_factory() as session:
        business_id = await _seed_business(session)
        other_business_id = await _seed_business(session)
        other_customer = await _seed_customer(
            session, other_business_id, entity_ref=None, first_paid_conversion_at=_NOW
        )
        await _seed_revenue_event(
            session,
            other_business_id,
            other_customer,
            kind="first_payment",
            amount_minor=_SUSPICIOUS_OUTSIDE_AMOUNT_MINOR,
            occurred_at=_NOW,
        )
        await session.commit()

    port = SqlCrmSummaryReadPort(mcp_session_factory, FixedClock(_NOW))

    summary = await port.get_crm_summary(str(business_id), window=CrmWindowPreset.THIRTY_DAYS)

    assert summary.total_revenue_minor is None
    assert summary.revenue_note == "cubo_con_menos_de_5_clientes"
    assert summary.revenue_by_channel == ()
