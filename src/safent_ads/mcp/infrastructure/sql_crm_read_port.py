"""`SqlCrmSummaryReadPort` real (R2, historia 12): unica lectura de CRM que
expone el MCP, agregada, nunca por cliente (D-6). El minimo de agregacion
k=5 se comprueba en SQL (`CASE WHEN count(...) >= 5 THEN ... END`), no en
presentacion: un cubo con menos de 5 clientes nunca sale de la base de
datos con su valor real."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp.application.company_read_ports import (
    SUPPRESSED_BUCKET_NOTE,
    ChannelRevenue,
    CrmSummary,
    CrmWindowPreset,
    crm_window_bounds,
)
from safent_ads.shared.clock import Clock

__all__ = ["SqlCrmSummaryReadPort"]

_DEFAULT_CURRENCY: Final = "EUR"
_ORGANIC_CHANNEL: Final = "organic"
_MIN_COHORT_SIZE: Final = 5

_NEW_CUSTOMERS_QUERY = text("""
    SELECT CASE WHEN count(*) >= 5 THEN count(*) END AS new_customers
      FROM customers
     WHERE business_id = :business_id
       AND first_paid_conversion_at >= :start_ts AND first_paid_conversion_at < :end_ts
""")

_RETURNING_CUSTOMERS_QUERY = text("""
    SELECT CASE WHEN count(DISTINCT customer_id) >= 5
                THEN count(DISTINCT customer_id) END AS returning_customers
      FROM revenue_events
     WHERE business_id = :business_id
       AND kind = 'recurring_payment'
       AND occurred_at >= :start_ts AND occurred_at < :end_ts
""")

_REVENUE_TOTALS_QUERY = text("""
    SELECT count(DISTINCT customer_id) AS distinct_customers,
           count(*) FILTER (WHERE kind IN ('first_payment', 'recurring_payment')) AS paid_orders,
           COALESCE(SUM(amount_minor), 0) AS net_revenue_minor,
           mode() WITHIN GROUP (ORDER BY currency) AS currency
      FROM revenue_events
     WHERE business_id = :business_id
       AND occurred_at >= :start_ts AND occurred_at < :end_ts
""")

_REVENUE_BY_CHANNEL_QUERY = text("""
    SELECT COALESCE(ae.platform, :organic_channel) AS channel,
           count(DISTINCT re.customer_id) AS distinct_customers,
           COALESCE(SUM(re.amount_minor), 0) AS revenue_minor
      FROM revenue_events re
      JOIN customers c ON c.id = re.customer_id AND c.business_id = re.business_id
      LEFT JOIN ad_entities ae ON ae.entity_ref = c.entity_ref AND ae.business_id = c.business_id
     WHERE re.business_id = :business_id
       AND re.occurred_at >= :start_ts AND re.occurred_at < :end_ts
     GROUP BY COALESCE(ae.platform, :organic_channel)
     ORDER BY channel
""")


class SqlCrmSummaryReadPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def get_crm_summary(self, business_id: str, *, window: CrmWindowPreset) -> CrmSummary:
        start, end = crm_window_bounds(window, self._clock.now().date())
        start_ts = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
        end_ts = datetime.combine(end, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
        params = {"business_id": business_id, "start_ts": start_ts, "end_ts": end_ts}
        async with self._session_factory() as session:
            new_customers = await _scalar(session, _NEW_CUSTOMERS_QUERY, params)
            returning_customers = await _scalar(session, _RETURNING_CUSTOMERS_QUERY, params)
            totals = (
                await session.execute(_REVENUE_TOTALS_QUERY, params)
            ).mappings().one()
            channel_rows = (
                await session.execute(
                    _REVENUE_BY_CHANNEL_QUERY, {**params, "organic_channel": _ORGANIC_CHANNEL}
                )
            ).mappings().all()
        return _build_summary(
            business_id=business_id,
            window=window,
            start=start,
            end=end,
            new_customers=new_customers,
            returning_customers=returning_customers,
            totals=totals,
            channel_rows=channel_rows,
        )


async def _scalar(session: AsyncSession, query: Any, params: dict[str, Any]) -> int | None:
    value = (await session.execute(query, params)).scalar_one()
    return None if value is None else int(value)


def _build_summary(
    *,
    business_id: str,
    window: CrmWindowPreset,
    start: Any,
    end: Any,
    new_customers: int | None,
    returning_customers: int | None,
    totals: Any,
    channel_rows: Any,
) -> CrmSummary:
    distinct_customers = int(totals["distinct_customers"])
    revenue_suppressed = distinct_customers < _MIN_COHORT_SIZE
    paid_orders = int(totals["paid_orders"])
    net_revenue_minor = int(totals["net_revenue_minor"])
    currency = str(totals["currency"] or _DEFAULT_CURRENCY)
    return CrmSummary(
        business_id=business_id,
        window_preset=window,
        window_start=start,
        window_end=end,
        currency=currency,
        new_customers=new_customers,
        new_customers_note=None if new_customers is not None else SUPPRESSED_BUCKET_NOTE,
        returning_customers=returning_customers,
        returning_customers_note=(
            None if returning_customers is not None else SUPPRESSED_BUCKET_NOTE
        ),
        total_revenue_minor=None if revenue_suppressed else net_revenue_minor,
        average_order_value_minor=(
            None
            if revenue_suppressed or paid_orders == 0
            else round(net_revenue_minor / paid_orders)
        ),
        lifetime_value_estimate_minor=(
            None
            if revenue_suppressed
            else round(net_revenue_minor / distinct_customers)
        ),
        revenue_note=SUPPRESSED_BUCKET_NOTE if revenue_suppressed else None,
        revenue_by_channel=tuple(_channel_revenue(row) for row in channel_rows),
    )


def _channel_revenue(row: Any) -> ChannelRevenue:
    suppressed = int(row["distinct_customers"]) < _MIN_COHORT_SIZE
    return ChannelRevenue(
        channel=str(row["channel"]),
        revenue_minor=None if suppressed else int(row["revenue_minor"]),
        note=SUPPRESSED_BUCKET_NOTE if suppressed else None,
    )
