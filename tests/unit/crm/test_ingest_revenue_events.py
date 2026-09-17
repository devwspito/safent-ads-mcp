"""`IngestRevenueEvents` (spec 027 T016): idempotencia por `source_event_id`,
`refund` positivo rechazado, LTV suma cobros y resta devoluciones."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.crm.application.customer_ports import RevenueEventRepository
from safent_ads.crm.application.ingest_revenue_events import (
    IngestRevenueEvents,
    RevenueEventIngestItem,
)
from safent_ads.crm.domain.customer import Customer
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import AttributionRung
from safent_ads.crm.testing.in_memory_repositories import (
    InMemoryCustomerRepository,
    InMemoryRevenueEventRepository,
)
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = BusinessId.new()
_DIGEST = "b" * 64
_CONNECTOR_ID = "connector-crm"
_OCCURRED_AT = datetime(2026, 3, 1, tzinfo=UTC)


async def _seeded_repos() -> tuple[InMemoryCustomerRepository, RevenueEventRepository]:
    customers = InMemoryCustomerRepository()
    revenue_events = InMemoryRevenueEventRepository()
    revenue_events.bind_customers(customers)
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
    return customers, revenue_events


def _item(**overrides: object) -> RevenueEventIngestItem:
    defaults: dict[str, object] = {
        "source_event_id": "evt-1",
        "identity_digest": _DIGEST,
        "kind": "first_payment",
        "amount_minor": 10_000,
        "currency": "EUR",
        "occurred_at": _OCCURRED_AT,
        "observed_at": _OCCURRED_AT,
        "mapping_version": 1,
    }
    defaults.update(overrides)
    return RevenueEventIngestItem(**defaults)  # type: ignore[arg-type]


async def test_new_event_is_ingested() -> None:
    customers, revenue_events = await _seeded_repos()
    use_case = IngestRevenueEvents(customers=customers, revenue_events=revenue_events)

    result = await use_case.execute(
        business_id=_BUSINESS_ID, connector_id=_CONNECTOR_ID, items=[_item()]
    )

    assert result.ingested == 1
    assert result.duplicated == 0


async def test_resending_the_same_batch_is_idempotent_not_duplicated() -> None:
    customers, revenue_events = await _seeded_repos()
    use_case = IngestRevenueEvents(customers=customers, revenue_events=revenue_events)
    await use_case.execute(business_id=_BUSINESS_ID, connector_id=_CONNECTOR_ID, items=[_item()])

    result = await use_case.execute(
        business_id=_BUSINESS_ID, connector_id=_CONNECTOR_ID, items=[_item()]
    )

    assert result.ingested == 0
    assert result.duplicated == 1


async def test_positive_refund_is_rejected() -> None:
    customers, revenue_events = await _seeded_repos()
    use_case = IngestRevenueEvents(customers=customers, revenue_events=revenue_events)

    result = await use_case.execute(
        business_id=_BUSINESS_ID,
        connector_id=_CONNECTOR_ID,
        items=[_item(source_event_id="evt-refund", kind="refund", amount_minor=500)],
    )

    assert result.ingested == 0
    assert result.rejected[0].code == "REFUND_MUST_BE_NEGATIVE"


async def test_unknown_customer_is_rejected() -> None:
    customers = InMemoryCustomerRepository()
    revenue_events = InMemoryRevenueEventRepository()
    revenue_events.bind_customers(customers)
    use_case = IngestRevenueEvents(customers=customers, revenue_events=revenue_events)

    result = await use_case.execute(
        business_id=_BUSINESS_ID, connector_id=_CONNECTOR_ID, items=[_item()]
    )

    assert result.rejected[0].code == "CUSTOMER_NOT_FOUND"


async def test_ltv_sums_payments_and_subtracts_refunds() -> None:
    customers, revenue_events = await _seeded_repos()
    use_case = IngestRevenueEvents(customers=customers, revenue_events=revenue_events)
    items = [
        _item(source_event_id="evt-first", kind="first_payment", amount_minor=10_000),
        _item(source_event_id="evt-r1", kind="recurring_payment", amount_minor=5_000),
        _item(source_event_id="evt-r2", kind="recurring_payment", amount_minor=5_000),
        _item(source_event_id="evt-r3", kind="recurring_payment", amount_minor=5_000),
        _item(source_event_id="evt-refund", kind="refund", amount_minor=-2_000),
    ]
    await use_case.execute(business_id=_BUSINESS_ID, connector_id=_CONNECTOR_ID, items=items)

    stats = await revenue_events.cohort_stats(business_id=_BUSINESS_ID, entity_ref=None)

    assert stats.observed_contribution_minor == 23_000
    assert stats.cohort_size == 1


async def test_currency_mismatch_is_rejected() -> None:
    customers, revenue_events = await _seeded_repos()
    use_case = IngestRevenueEvents(customers=customers, revenue_events=revenue_events)

    result = await use_case.execute(
        business_id=_BUSINESS_ID,
        connector_id=_CONNECTOR_ID,
        items=[_item(currency="USD")],
    )

    assert result.rejected[0].code == "CURRENCY_MISMATCH"
