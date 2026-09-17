"""`ForgetCustomer` (spec 027 A-2): borra en las tres tablas y anota la
supresion sin el identificador crudo."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.crm.application.forget_customer import ForgetCustomer
from safent_ads.crm.domain.customer import Customer
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.identity_mapping import IdentityMapping
from safent_ads.crm.domain.lead_attribution import AttributionRung
from safent_ads.crm.domain.revenue_event import RevenueEvent, RevenueEventId, RevenueEventKind
from safent_ads.crm.testing.in_memory_repositories import (
    InMemoryCustomerForgottenRecorder,
    InMemoryCustomerRepository,
    InMemoryIdentityMappingRepository,
    InMemoryRevenueEventRepository,
)
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_DIGEST = "c" * 64
_OCCURRED_AT = datetime(2026, 3, 1, tzinfo=UTC)


async def _seeded() -> tuple[
    InMemoryCustomerRepository,
    InMemoryRevenueEventRepository,
    InMemoryIdentityMappingRepository,
    InMemoryCustomerForgottenRecorder,
]:
    customers = InMemoryCustomerRepository()
    revenue_events = InMemoryRevenueEventRepository()
    revenue_events.bind_customers(customers)
    identity_mappings = InMemoryIdentityMappingRepository()
    audit = InMemoryCustomerForgottenRecorder()

    identity = HashedIdentity.from_digest(business_id=_BUSINESS_ID, digest=_DIGEST)
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
            source_event_id="evt-1",
            mapping_version=1,
        ),
        business_id=_BUSINESS_ID,
        connector_id="connector-crm",
    )
    await identity_mappings.record(
        IdentityMapping(
            business_id=_BUSINESS_ID,
            identity_digest=_DIGEST,
            salt_version=1,
            observed_at=_OCCURRED_AT,
        )
    )
    return customers, revenue_events, identity_mappings, audit


def _use_case(
    customers: InMemoryCustomerRepository,
    revenue_events: InMemoryRevenueEventRepository,
    identity_mappings: InMemoryIdentityMappingRepository,
    audit: InMemoryCustomerForgottenRecorder,
) -> ForgetCustomer:
    return ForgetCustomer(
        customers=customers,
        revenue_events=revenue_events,
        identity_mappings=identity_mappings,
        audit=audit,
    )


async def test_forgetting_a_customer_deletes_rows_in_all_three_tables() -> None:
    customers, revenue_events, identity_mappings, audit = await _seeded()
    use_case = _use_case(customers, revenue_events, identity_mappings, audit)

    result = await use_case.execute(business_id=_BUSINESS_ID, identity_digest=_DIGEST)

    assert result.rows_deleted == 3  # 1 customer + 1 revenue_event + 1 identity_mapping
    found = await customers.find_by_identity(business_id=_BUSINESS_ID, identity_digest=_DIGEST)
    assert found is None
    stats = await revenue_events.cohort_stats(business_id=_BUSINESS_ID, entity_ref=None)
    assert stats.cohort_size == 0


async def test_forgetting_records_the_audit_entry_without_a_raw_identifier() -> None:
    customers, revenue_events, identity_mappings, audit = await _seeded()
    use_case = _use_case(customers, revenue_events, identity_mappings, audit)

    await use_case.execute(business_id=_BUSINESS_ID, identity_digest=_DIGEST)

    assert len(audit.recorded) == 1
    recorded_business_id, recorded_hash, rows_deleted = audit.recorded[0]
    assert recorded_business_id == _BUSINESS_ID
    assert recorded_hash == _DIGEST
    assert rows_deleted == 3


async def test_forgetting_an_unknown_customer_is_a_no_op_that_still_records() -> None:
    customers = InMemoryCustomerRepository()
    revenue_events = InMemoryRevenueEventRepository()
    revenue_events.bind_customers(customers)
    identity_mappings = InMemoryIdentityMappingRepository()
    audit = InMemoryCustomerForgottenRecorder()
    use_case = _use_case(customers, revenue_events, identity_mappings, audit)

    result = await use_case.execute(business_id=_BUSINESS_ID, identity_digest=_DIGEST)

    assert result.rows_deleted == 0
    assert len(audit.recorded) == 1
