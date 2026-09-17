"""`ComputeCustomerValue` (spec 027, contracts/crm-link.md §3): sin volumen
suficiente, ningun numero -- nunca un cero disfrazado de dato."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.crm.application.compute_customer_value import ComputeCustomerValue
from safent_ads.crm.domain.customer import Customer
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import AttributionRung
from safent_ads.crm.domain.revenue_event import RevenueEvent, RevenueEventId, RevenueEventKind
from safent_ads.crm.testing.in_memory_repositories import (
    InMemoryCustomerRepository,
    InMemoryRevenueEventRepository,
)
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_OCCURRED_AT = datetime(2026, 3, 1, tzinfo=UTC)


async def _repos_with_customers(
    count: int,
) -> tuple[InMemoryCustomerRepository, InMemoryRevenueEventRepository]:
    customers = InMemoryCustomerRepository()
    revenue_events = InMemoryRevenueEventRepository()
    revenue_events.bind_customers(customers)
    for i in range(count):
        digest = f"{i:064x}"
        identity = HashedIdentity.from_digest(business_id=_BUSINESS_ID, digest=digest)
        customer = Customer.first_seen(
            business_id=_BUSINESS_ID,
            hashed_identity=identity,
            entity_ref=None,
            attribution_rung=AttributionRung.AGGREGATE,
            currency="EUR",
            seen_at=_OCCURRED_AT,
        )
        await customers.upsert(customer)
        await revenue_events.insert_if_new(
            RevenueEvent(
                revenue_event_id=RevenueEventId.new(),
                customer_id=customer.customer_id,
                kind=RevenueEventKind.FIRST_PAYMENT,
                amount_minor=10_000,
                currency="EUR",
                occurred_at=_OCCURRED_AT,
                observed_at=_OCCURRED_AT,
                source_event_id=f"evt-{i}",
                mapping_version=1,
            ),
            business_id=_BUSINESS_ID,
            connector_id="connector-crm",
        )
    return customers, revenue_events


async def test_immature_cohort_returns_no_number_with_a_reason() -> None:
    _, revenue_events = await _repos_with_customers(3)
    use_case = ComputeCustomerValue(revenue_events=revenue_events)

    value = await use_case.execute(business_id=_BUSINESS_ID)

    assert value.is_provisional is True
    assert value.projected_contribution_minor is None
    assert "3" in (value.no_number_reason or "")


async def test_sufficient_cohort_returns_an_observed_number() -> None:
    _, revenue_events = await _repos_with_customers(5)
    use_case = ComputeCustomerValue(revenue_events=revenue_events)

    value = await use_case.execute(business_id=_BUSINESS_ID)

    assert value.is_provisional is False
    assert value.observed_contribution_minor == 50_000
    assert value.cohort_size == 5


async def test_empty_cohort_returns_no_number() -> None:
    revenue_events = InMemoryRevenueEventRepository()
    use_case = ComputeCustomerValue(revenue_events=revenue_events)

    value = await use_case.execute(business_id=_BUSINESS_ID)

    assert value.is_provisional is True
    assert value.cohort_size == 0
