"""`InMemoryLeadAttributionRepository`: doble de contrato de
`crm.application.ports.LeadAttributionRepository` (tests/contracts/crm/).

Tambien los dobles de `customer_ports` (spec 027 T015): `InMemoryCustomer
Repository`, `InMemoryRevenueEventRepository`, `InMemoryIdentityMapping
Repository`, `InMemoryCrmBridgeHealthRepository`, `InMemoryCustomer
ForgottenRecorder`."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from safent_ads.crm.application.customer_ports import RevenueCohortStats
from safent_ads.crm.domain.bridge_health import CrmBridgeHealth
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.customer import Customer, CustomerId
from safent_ads.crm.domain.identity_mapping import IdentityMapping
from safent_ads.crm.domain.lead_attribution import LeadAttribution
from safent_ads.crm.domain.revenue_event import RevenueEvent
from safent_ads.shared.ids import BusinessId

__all__ = [
    "InMemoryCrmBridgeHealthRepository",
    "InMemoryCustomerForgottenRecorder",
    "InMemoryCustomerRepository",
    "InMemoryIdentityMappingRepository",
    "InMemoryLeadAttributionRepository",
    "InMemoryRevenueEventRepository",
]


class InMemoryLeadAttributionRepository:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str, datetime], LeadAttribution] = {}

    async def save(self, attribution: LeadAttribution) -> bool:
        key = (
            str(attribution.business_id.value),
            attribution.hashed_identity.digest if attribution.hashed_identity else "",
            attribution.conversion_kind.value,
            attribution.occurred_at,
        )
        if key in self._rows:
            return False
        self._rows[key] = attribution
        return True

    async def find_for_calendar_events(
        self, *, business_id: BusinessId, calendar_event_ids: Sequence[str]
    ) -> Sequence[LeadAttribution]:
        wanted = set(calendar_event_ids)
        return sorted(
            (
                a
                for a in self._rows.values()
                if a.business_id == business_id and a.calendar_event_id in wanted
            ),
            key=lambda a: a.occurred_at,
        )

    async def count_by_kind_in_window(
        self,
        *,
        business_id: BusinessId,
        conversion_kind: ConversionKind,
        window_start: date,
        window_end: date,
        entity_ref: str | None = None,
    ) -> int:
        return sum(
            1
            for a in self._rows.values()
            if a.business_id == business_id
            and a.conversion_kind is conversion_kind
            and window_start <= a.occurred_at.date() < window_end
            and (entity_ref is None or str(a.entity_ref) == entity_ref)
        )

    async def last_event_at(
        self, *, business_id: BusinessId, conversion_kind: ConversionKind
    ) -> datetime | None:
        matches = [
            a.occurred_at
            for a in self._rows.values()
            if a.business_id == business_id and a.conversion_kind is conversion_kind
        ]
        return max(matches) if matches else None

    async def list_distinct_entity_refs_in_window(
        self, *, business_id: BusinessId, window_start: date, window_end: date
    ) -> Sequence[str]:
        return sorted(
            {
                str(a.entity_ref)
                for a in self._rows.values()
                if a.business_id == business_id
                and a.entity_ref is not None
                and window_start <= a.occurred_at.date() < window_end
            }
        )


class InMemoryCustomerRepository:
    def __init__(self) -> None:
        self._by_identity: dict[tuple[str, str], Customer] = {}

    async def find_by_identity(
        self, *, business_id: BusinessId, identity_digest: str
    ) -> Customer | None:
        return self._by_identity.get((str(business_id), identity_digest))

    async def upsert(self, customer: Customer) -> None:
        key = (str(customer.business_id), customer.hashed_identity.digest)
        self._by_identity[key] = customer

    async def delete_for_identity(self, *, business_id: BusinessId, identity_digest: str) -> int:
        key = (str(business_id), identity_digest)
        return 1 if self._by_identity.pop(key, None) is not None else 0

    async def count_active_in_window(
        self,
        *,
        business_id: BusinessId,
        entity_ref: str | None,
        window_start: datetime,
        window_end: datetime,
    ) -> int:
        return sum(
            1
            for customer in self._by_identity.values()
            if customer.business_id == business_id
            and customer.first_paid_conversion_at is not None
            and window_start <= customer.first_paid_conversion_at < window_end
            and (entity_ref is None or str(customer.entity_ref) == entity_ref)
        )


class InMemoryRevenueEventRepository:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], tuple[RevenueEvent, BusinessId, str]] = {}
        self._customers: InMemoryCustomerRepository | None = None

    def bind_customers(self, customers: InMemoryCustomerRepository) -> None:
        """Doble de prueba: necesita mirar `Customer.entity_ref`/`currency`
        para `cohort_stats`, igual que la consulta SQL hace un JOIN real."""
        self._customers = customers

    async def insert_if_new(
        self, event: RevenueEvent, *, business_id: BusinessId, connector_id: str
    ) -> bool:
        key = (str(business_id), connector_id, event.source_event_id)
        if key in self._rows:
            return False
        self._rows[key] = (event, business_id, connector_id)
        return True

    async def delete_for_customer(self, *, business_id: BusinessId, customer_id: CustomerId) -> int:
        to_delete = [
            key
            for key, (event, row_business_id, _) in self._rows.items()
            if row_business_id == business_id and event.customer_id == customer_id
        ]
        for key in to_delete:
            del self._rows[key]
        return len(to_delete)

    async def cohort_stats(
        self, *, business_id: BusinessId, entity_ref: str | None
    ) -> RevenueCohortStats:
        customers_by_id = await self._customers_by_id(business_id)
        matching = [
            event
            for event, row_business_id, _ in self._rows.values()
            if row_business_id == business_id
            and _matches_entity(customers_by_id.get(event.customer_id), entity_ref)
        ]
        currency = matching[0].currency if matching else None
        cohort = {event.customer_id for event in matching}
        observed = sum(event.amount_minor for event in matching)
        return RevenueCohortStats(
            cohort_size=len(cohort), observed_contribution_minor=observed, currency=currency
        )

    async def last_event_at(self, *, business_id: BusinessId, connector_id: str) -> datetime | None:
        matches = [
            event.occurred_at
            for event, row_business_id, row_connector_id in self._rows.values()
            if row_business_id == business_id and row_connector_id == connector_id
        ]
        return max(matches) if matches else None

    async def _customers_by_id(self, business_id: BusinessId) -> dict[CustomerId, Customer]:
        if self._customers is None:
            return {}
        return {
            customer.customer_id: customer
            for customer in self._customers._by_identity.values()  # noqa: SLF001 - doble de prueba
            if customer.business_id == business_id
        }


def _matches_entity(customer: Customer | None, entity_ref: str | None) -> bool:
    if entity_ref is None:
        return True
    return customer is not None and str(customer.entity_ref) == entity_ref


class InMemoryIdentityMappingRepository:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, int], IdentityMapping] = {}

    async def record(self, mapping: IdentityMapping) -> None:
        key = (str(mapping.business_id), mapping.identity_digest, mapping.salt_version)
        self._rows[key] = mapping

    async def delete_for_identity(self, *, business_id: BusinessId, identity_digest: str) -> int:
        to_delete = [
            key
            for key in self._rows
            if key[0] == str(business_id) and key[1] == identity_digest
        ]
        for key in to_delete:
            del self._rows[key]
        return len(to_delete)


class InMemoryCrmBridgeHealthRepository:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], CrmBridgeHealth] = {}

    async def upsert(self, health: CrmBridgeHealth) -> None:
        self._rows[(str(health.business_id), health.connector_id)] = health

    async def get_for_business(self, *, business_id: BusinessId) -> CrmBridgeHealth | None:
        """Si hay mas de un puente para el negocio, devuelve el peor
        (`has_recent_events_24h = false` primero) -- mismo contrato que
        `SqlCrmBridgeHealthRepository.get_for_business`: el freeze gate
        nunca debe leer un puente sano mientras otro esta roto."""
        matches = [
            health
            for (row_business_id, _), health in self._rows.items()
            if row_business_id == str(business_id)
        ]
        if not matches:
            return None
        return min(
            matches,
            key=lambda health: (health.has_recent_events_24h, -health.updated_at.timestamp()),
        )


class InMemoryCustomerForgottenRecorder:
    def __init__(self) -> None:
        self.recorded: list[tuple[BusinessId, str, int]] = []

    async def record(
        self, *, business_id: BusinessId, customer_hash: str, rows_deleted: int
    ) -> None:
        self.recorded.append((business_id, customer_hash, rows_deleted))
