"""Banco de pruebas de contrato de `LeadAttributionRepository` (T145): los
mismos casos corren contra el doble en memoria y contra `SqlLeadAttribution
Repository` sobre Postgres real (mismo patron que `tests/contracts/accounts`)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.crm.application.customer_ports import (
    CrmBridgeHealthRepository,
    CustomerRepository,
    IdentityMappingRepository,
    RevenueEventRepository,
)
from safent_ads.crm.application.ports import LeadAttributionRepository
from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.domain.hashed_identity import HashedIdentity
from safent_ads.crm.domain.lead_attribution import (
    AttributionRung,
    LeadAttribution,
    LeadAttributionId,
)
from safent_ads.crm.infrastructure.sql_crm_bridge_health_repository import (
    SqlCrmBridgeHealthRepository,
)
from safent_ads.crm.infrastructure.sql_customer_repository import SqlCustomerRepository
from safent_ads.crm.infrastructure.sql_identity_mapping_repository import (
    SqlIdentityMappingRepository,
)
from safent_ads.crm.infrastructure.sql_repositories import SqlLeadAttributionRepository
from safent_ads.crm.infrastructure.sql_revenue_event_repository import SqlRevenueEventRepository
from safent_ads.crm.testing.in_memory_repositories import (
    InMemoryCrmBridgeHealthRepository,
    InMemoryCustomerRepository,
    InMemoryIdentityMappingRepository,
    InMemoryLeadAttributionRepository,
    InMemoryRevenueEventRepository,
)
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.conftest import rolled_back_session
from tests.contracts.sql_fixtures import seed_entity


class RepositoryFixture(Protocol):
    attributions: LeadAttributionRepository

    async def given_business(self, business_id: BusinessId) -> None: ...

    async def given_calendar_event(
        self, business_id: BusinessId, calendar_event_id: str, offering_id: str | None = None
    ) -> None: ...

    async def given_entity(self, entity_ref: EntityRef) -> BusinessId: ...


@dataclass(slots=True)
class InMemoryFixture:
    attributions: LeadAttributionRepository

    async def given_business(self, business_id: BusinessId) -> None:
        del business_id

    async def given_calendar_event(
        self, business_id: BusinessId, calendar_event_id: str, offering_id: str | None = None
    ) -> None:
        del business_id, calendar_event_id, offering_id

    async def given_entity(self, entity_ref: EntityRef) -> BusinessId:
        del entity_ref
        return new_business_id()


@dataclass(slots=True)
class SqlFixture:
    attributions: LeadAttributionRepository
    session: AsyncSession

    async def given_business(self, business_id: BusinessId) -> None:
        await self.session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio de contrato', 'Europe/Madrid', 'EUR') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": business_id.value, "slug": f"crm-contrato-{business_id.value.hex[:12]}"},
        )
        await self.session.flush()

    async def given_calendar_event(
        self, business_id: BusinessId, calendar_event_id: str, offering_id: str | None = None
    ) -> None:
        if offering_id is None:
            offering_id = str(uuid.uuid4())
            await self.session.execute(
                text(
                    "INSERT INTO offerings (id, business_id, code, title) "
                    "VALUES (:id, :business_id, :code, 'Oferta de contrato de prueba')"
                ),
                {
                    "id": offering_id,
                    "business_id": business_id.value,
                    "code": f"off-{offering_id[:10]}",
                },
            )
        await self.session.execute(
            text(
                "INSERT INTO calendar_events "
                "(id, business_id, offering_id, name, window_start, window_end, source) "
                "VALUES (:id, :business_id, :offering_id, 'Evento de calendario de contrato', "
                "'2026-01-01', '2026-06-01', 'contrato')"
            ),
            {"id": calendar_event_id, "business_id": business_id.value, "offering_id": offering_id},
        )
        await self.session.flush()

    async def given_entity(self, entity_ref: EntityRef) -> BusinessId:
        business_id = await seed_entity(self.session, entity_ref)
        return BusinessId(business_id)


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def repositories(request: pytest.FixtureRequest) -> AsyncIterator[RepositoryFixture]:
    if request.param == "in_memory":
        yield InMemoryFixture(attributions=InMemoryLeadAttributionRepository())
        return
    database_url: str = request.getfixturevalue("database_url")
    async with rolled_back_session(database_url) as session:
        yield SqlFixture(attributions=SqlLeadAttributionRepository(session), session=session)


def new_business_id() -> BusinessId:
    return BusinessId(uuid.uuid4())


def hashed_identity(business_id: BusinessId, raw: str = "lead@example.com") -> HashedIdentity:
    return HashedIdentity.compute(business_id=business_id, raw_identifier=raw, salt="contract-test")


def build_attribution(
    business_id: BusinessId,
    *,
    conversion_kind: ConversionKind = ConversionKind.BUSINESS_CONVERSION,
    occurred_at: datetime | None = None,
    calendar_event_id: str | None = None,
    value_minor: int = 120_000,
    raw_identity: str = "lead@example.com",
    entity_ref: EntityRef | None = None,
) -> LeadAttribution:
    return LeadAttribution(
        lead_attribution_id=LeadAttributionId.new(),
        business_id=business_id,
        hashed_identity=hashed_identity(business_id, raw_identity),
        entity_ref=entity_ref,
        attribution_rung=(
            AttributionRung.AGGREGATE if entity_ref is None else AttributionRung.HASHED_IDENTITY
        ),
        conversion_kind=conversion_kind,
        value_minor=value_minor,
        occurred_at=occurred_at or datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
        observed_at=occurred_at or datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
        calendar_event_id=calendar_event_id,
    )


def digest_of(raw: str, *, salt: str = "contract-test") -> str:
    return hashlib.sha256(f"{salt}:{raw.strip().lower()}".encode()).hexdigest()


# -- Banco de contrato de `customers`/`revenue_events`/`identity_mappings`/
# `crm_bridge_health` (spec 027 T015) --------------------------------------


class CustomerRepositoryFixture(Protocol):
    customers: CustomerRepository
    revenue_events: RevenueEventRepository
    identity_mappings: IdentityMappingRepository
    bridge_health: CrmBridgeHealthRepository

    async def given_business(self) -> BusinessId: ...


@dataclass(slots=True)
class CustomerInMemoryFixture:
    customers: CustomerRepository
    revenue_events: RevenueEventRepository
    identity_mappings: IdentityMappingRepository
    bridge_health: CrmBridgeHealthRepository

    async def given_business(self) -> BusinessId:
        return BusinessId.new()


@dataclass(slots=True)
class CustomerSqlFixture:
    customers: CustomerRepository
    revenue_events: RevenueEventRepository
    identity_mappings: IdentityMappingRepository
    bridge_health: CrmBridgeHealthRepository
    session: AsyncSession

    async def given_business(self) -> BusinessId:
        business_id = uuid.uuid4()
        await self.session.execute(
            text(
                "INSERT INTO businesses (id, slug, name, timezone, reference_currency) "
                "VALUES (:id, :slug, 'Negocio de contrato', 'Europe/Madrid', 'EUR')"
            ),
            {"id": business_id, "slug": f"crm-customers-{business_id.hex[:12]}"},
        )
        await self.session.flush()
        return BusinessId(business_id)


def _customer_in_memory_fixture() -> CustomerInMemoryFixture:
    customers = InMemoryCustomerRepository()
    revenue_events = InMemoryRevenueEventRepository()
    revenue_events.bind_customers(customers)
    return CustomerInMemoryFixture(
        customers=customers,
        revenue_events=revenue_events,
        identity_mappings=InMemoryIdentityMappingRepository(),
        bridge_health=InMemoryCrmBridgeHealthRepository(),
    )


@pytest.fixture(
    params=[
        pytest.param("in_memory", id="in_memory"),
        pytest.param("sql", id="sql", marks=pytest.mark.integration),
    ]
)
async def customer_repositories(
    request: pytest.FixtureRequest,
) -> AsyncIterator[CustomerRepositoryFixture]:
    if request.param == "in_memory":
        yield _customer_in_memory_fixture()
        return
    database_url: str = request.getfixturevalue("database_url")
    async with rolled_back_session(database_url) as session:
        yield CustomerSqlFixture(
            customers=SqlCustomerRepository(session),
            revenue_events=SqlRevenueEventRepository(session),
            identity_mappings=SqlIdentityMappingRepository(session),
            bridge_health=SqlCrmBridgeHealthRepository(session),
            session=session,
        )
