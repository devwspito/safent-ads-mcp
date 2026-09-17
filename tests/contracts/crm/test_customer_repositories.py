"""Contrato de `CustomerRepository`/`RevenueEventRepository`/
`IdentityMappingRepository`/`CrmBridgeHealthRepository` (spec 027 T015):
identico en memoria y contra Postgres real."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.crm.domain.bridge_health import ConnectorBridgeState, CrmBridgeHealth
from safent_ads.crm.domain.customer import Customer
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.identity_mapping import IdentityMapping
from safent_ads.crm.domain.lead_attribution import AttributionRung
from safent_ads.crm.domain.revenue_event import RevenueEvent, RevenueEventId, RevenueEventKind
from tests.contracts.crm.conftest import CustomerRepositoryFixture

_OCCURRED_AT = datetime(2026, 3, 1, tzinfo=UTC)
_CONNECTOR_ID = "connector-crm"


def _customer(business_id, digest: str = "a" * 64) -> Customer:
    return Customer.first_seen(
        business_id=business_id,
        hashed_identity=HashedIdentity.from_digest(business_id=business_id, digest=digest),
        entity_ref=None,
        attribution_rung=AttributionRung.AGGREGATE,
        currency="EUR",
        seen_at=_OCCURRED_AT,
    )


def _event(customer: Customer, *, source_event_id: str, amount_minor: int = 10_000) -> RevenueEvent:
    return RevenueEvent(
        revenue_event_id=RevenueEventId.new(),
        customer_id=customer.customer_id,
        kind=RevenueEventKind.FIRST_PAYMENT,
        amount_minor=amount_minor,
        currency="EUR",
        occurred_at=_OCCURRED_AT,
        observed_at=_OCCURRED_AT,
        source_event_id=source_event_id,
        mapping_version=1,
    )


async def test_customer_round_trips_by_identity(
    customer_repositories: CustomerRepositoryFixture,
) -> None:
    business_id = await customer_repositories.given_business()
    customer = _customer(business_id)

    await customer_repositories.customers.upsert(customer)
    found = await customer_repositories.customers.find_by_identity(
        business_id=business_id, identity_digest=customer.hashed_identity.digest
    )

    assert found is not None
    assert found.customer_id == customer.customer_id
    assert found.state == customer.state


async def test_revenue_event_insert_if_new_is_idempotent(
    customer_repositories: CustomerRepositoryFixture,
) -> None:
    business_id = await customer_repositories.given_business()
    customer = _customer(business_id)
    await customer_repositories.customers.upsert(customer)
    event = _event(customer, source_event_id="evt-1")

    first = await customer_repositories.revenue_events.insert_if_new(
        event, business_id=business_id, connector_id=_CONNECTOR_ID
    )
    second = await customer_repositories.revenue_events.insert_if_new(
        event, business_id=business_id, connector_id=_CONNECTOR_ID
    )

    assert first is True
    assert second is False


async def test_cohort_stats_sums_only_this_business(
    customer_repositories: CustomerRepositoryFixture,
) -> None:
    mine = await customer_repositories.given_business()
    other = await customer_repositories.given_business()
    my_customer = _customer(mine, digest="1" * 64)
    other_customer = _customer(other, digest="2" * 64)
    await customer_repositories.customers.upsert(my_customer)
    await customer_repositories.customers.upsert(other_customer)
    await customer_repositories.revenue_events.insert_if_new(
        _event(my_customer, source_event_id="evt-mine"),
        business_id=mine,
        connector_id=_CONNECTOR_ID,
    )
    await customer_repositories.revenue_events.insert_if_new(
        _event(other_customer, source_event_id="evt-other"),
        business_id=other,
        connector_id=_CONNECTOR_ID,
    )

    stats = await customer_repositories.revenue_events.cohort_stats(
        business_id=mine, entity_ref=None
    )

    assert stats.cohort_size == 1
    assert stats.observed_contribution_minor == 10_000


async def test_forgetting_a_customer_deletes_revenue_events_and_identity_mappings(
    customer_repositories: CustomerRepositoryFixture,
) -> None:
    business_id = await customer_repositories.given_business()
    customer = _customer(business_id)
    await customer_repositories.customers.upsert(customer)
    await customer_repositories.revenue_events.insert_if_new(
        _event(customer, source_event_id="evt-1"),
        business_id=business_id,
        connector_id=_CONNECTOR_ID,
    )
    await customer_repositories.identity_mappings.record(
        IdentityMapping(
            business_id=business_id,
            identity_digest=customer.hashed_identity.digest,
            salt_version=1,
            observed_at=_OCCURRED_AT,
        )
    )

    revenue_deleted = await customer_repositories.revenue_events.delete_for_customer(
        business_id=business_id, customer_id=customer.customer_id
    )
    mapping_deleted = await customer_repositories.identity_mappings.delete_for_identity(
        business_id=business_id, identity_digest=customer.hashed_identity.digest
    )
    customer_deleted = await customer_repositories.customers.delete_for_identity(
        business_id=business_id, identity_digest=customer.hashed_identity.digest
    )

    assert revenue_deleted == 1
    assert mapping_deleted == 1
    assert customer_deleted == 1
    assert (
        await customer_repositories.customers.find_by_identity(
            business_id=business_id, identity_digest=customer.hashed_identity.digest
        )
        is None
    )


async def test_bridge_health_worst_row_wins_when_several_connectors_exist(
    customer_repositories: CustomerRepositoryFixture,
) -> None:
    business_id = await customer_repositories.given_business()
    healthy = CrmBridgeHealth.evaluate(
        business_id=business_id,
        connector_id="connector-healthy",
        connector_state=ConnectorBridgeState.READY,
        last_event_at=_OCCURRED_AT,
        as_of=_OCCURRED_AT,
        cause=None,
    )
    broken = CrmBridgeHealth.evaluate(
        business_id=business_id,
        connector_id="connector-broken",
        connector_state=ConnectorBridgeState.DEGRADED,
        last_event_at=_OCCURRED_AT - timedelta(hours=48),
        as_of=_OCCURRED_AT,
        cause="credential_expired",
    )
    await customer_repositories.bridge_health.upsert(healthy)
    await customer_repositories.bridge_health.upsert(broken)

    worst = await customer_repositories.bridge_health.get_for_business(business_id=business_id)

    assert worst is not None
    assert worst.has_recent_events_24h is False


async def test_unconfigured_bridge_returns_none(
    customer_repositories: CustomerRepositoryFixture,
) -> None:
    business_id = await customer_repositories.given_business()

    result = await customer_repositories.bridge_health.get_for_business(business_id=business_id)

    assert result is None
