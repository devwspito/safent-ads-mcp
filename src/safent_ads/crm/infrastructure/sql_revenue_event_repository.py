"""`SqlRevenueEventRepository` sobre `revenue_events` (0032_revenue_events,
spec 027): idempotente por esquema, `cohort_stats` agrega sin traer una
sola fila con un digest (FR-009 de spec 026)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.crm.application.customer_ports import RevenueCohortStats
from safent_ads.crm.domain.customer import CustomerId
from safent_ads.crm.domain.revenue_event import RevenueEvent
from safent_ads.shared.ids import BusinessId

__all__ = ["SqlRevenueEventRepository"]

_INSERT = text("""
    INSERT INTO revenue_events (
        id, business_id, connector_id, customer_id, source_event_id, kind, amount_minor,
        currency, occurred_at, observed_at, mapping_version
    ) VALUES (
        :id, :business_id, :connector_id, :customer_id, :source_event_id, :kind, :amount_minor,
        :currency, :occurred_at, :observed_at, :mapping_version
    )
    ON CONFLICT ON CONSTRAINT revenue_events_idempotency_unique DO NOTHING
    RETURNING id
""")

_DELETE_FOR_CUSTOMER = text(
    "DELETE FROM revenue_events WHERE business_id = :business_id AND customer_id = :customer_id "
    "RETURNING id"
)

_COHORT_STATS = text("""
    SELECT count(DISTINCT re.customer_id) AS cohort_size,
           COALESCE(sum(re.amount_minor), 0) AS observed_contribution_minor,
           max(re.currency) AS currency
      FROM revenue_events re
      JOIN customers c ON c.id = re.customer_id
     WHERE re.business_id = :business_id
       AND (CAST(:entity_ref AS TEXT) IS NULL OR c.entity_ref = :entity_ref)
""")

_LAST_EVENT_AT = text("""
    SELECT max(occurred_at) AS last_event_at
      FROM revenue_events
     WHERE business_id = :business_id AND connector_id = :connector_id
""")


class SqlRevenueEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_if_new(
        self, event: RevenueEvent, *, business_id: BusinessId, connector_id: str
    ) -> bool:
        result = await self._session.execute(
            _INSERT,
            {
                "id": event.revenue_event_id.value,
                "business_id": business_id.value,
                "connector_id": connector_id,
                "customer_id": event.customer_id.value,
                "source_event_id": event.source_event_id,
                "kind": event.kind.value,
                "amount_minor": event.amount_minor,
                "currency": event.currency,
                "occurred_at": event.occurred_at,
                "observed_at": event.observed_at,
                "mapping_version": event.mapping_version,
            },
        )
        await self._session.flush()
        return result.mappings().one_or_none() is not None

    async def delete_for_customer(self, *, business_id: BusinessId, customer_id: CustomerId) -> int:
        result = await self._session.execute(
            _DELETE_FOR_CUSTOMER,
            {"business_id": business_id.value, "customer_id": customer_id.value},
        )
        await self._session.flush()
        return len(result.mappings().all())

    async def cohort_stats(
        self, *, business_id: BusinessId, entity_ref: str | None
    ) -> RevenueCohortStats:
        result = await self._session.execute(
            _COHORT_STATS, {"business_id": business_id.value, "entity_ref": entity_ref}
        )
        row = result.mappings().one()
        return RevenueCohortStats(
            cohort_size=int(row["cohort_size"]),
            observed_contribution_minor=int(row["observed_contribution_minor"]),
            currency=row["currency"],
        )

    async def last_event_at(self, *, business_id: BusinessId, connector_id: str) -> datetime | None:
        result = await self._session.execute(
            _LAST_EVENT_AT, {"business_id": business_id.value, "connector_id": connector_id}
        )
        return result.scalar_one_or_none()
