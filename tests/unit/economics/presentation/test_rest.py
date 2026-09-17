"""Rutas REST de `economics` (contracts/rest-api.md §Economia unitaria)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from safent_ads.economics.application.query_service import EconomicsQueryService
from safent_ads.economics.domain.identifiers import ProductId, UnitEconomicsProfileId
from safent_ads.economics.domain.money import Money
from safent_ads.economics.domain.rate import Rate, Theta
from safent_ads.economics.domain.unit_economics import UnitEconomicsProfile
from safent_ads.economics.presentation.rest import build_economics_router
from safent_ads.economics.testing.in_memory_repositories import (
    InMemoryLagCurveRepository,
    InMemoryPlatformDivergenceRepository,
    InMemoryUnitEconomicsProfileRepository,
)
from safent_ads.iam.presentation.dependencies import require_business_access
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId

_BUSINESS_ID = uuid.uuid4()
_PRODUCT_ID = uuid.uuid4()


async def _pass_through_business_access(business_id: uuid.UUID) -> uuid.UUID:
    """Sustituye la sesion `iam` real: esta suite prueba el router, no la
    autenticacion (que ya tiene su propia suite en `tests/unit/iam`)."""
    return business_id


@pytest.fixture
def profiles() -> InMemoryUnitEconomicsProfileRepository:
    return InMemoryUnitEconomicsProfileRepository()


@pytest.fixture
def service(profiles: InMemoryUnitEconomicsProfileRepository) -> EconomicsQueryService:
    return EconomicsQueryService(
        profiles=profiles,
        lag_curves=InMemoryLagCurveRepository(),
        divergences=InMemoryPlatformDivergenceRepository(),
        clock=FixedClock(datetime(2026, 6, 1, tzinfo=UTC)),
    )


@pytest.fixture
def client(service: EconomicsQueryService) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(build_economics_router(service))
    app.dependency_overrides[require_business_access] = _pass_through_business_access
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


async def _seed_profile(profiles: InMemoryUnitEconomicsProfileRepository) -> None:
    await profiles.save(
        UnitEconomicsProfile.create(
            profile_id=UnitEconomicsProfileId.new(),
            business_id=BusinessId(_BUSINESS_ID),
            product_id=ProductId(_PRODUCT_ID),
            version=1,
            effective_from=date(2026, 1, 1),
            list_price=Money.of("1200"),
            vat_rate=Rate.zero(),
            discount_rate=Rate.of("0.08"),
            refund_rate=Rate.of("0.06"),
            delivery_cost=Money.of("90"),
            sales_cost_per_close=Money.of("140"),
            collection_rate=Rate.of("0.92"),
            cvr_lead_to_business_conversion=Rate.of("0.045"),
            theta=Theta(Decimal("0.35")),
            margin_horizon_days=90,
        )
    )


class TestUnitEconomicsRoutes:
    async def test_returns_view_when_profile_exists(
        self, client: TestClient, profiles: InMemoryUnitEconomicsProfileRepository
    ) -> None:
        await _seed_profile(profiles)

        response = client.get(
            "/api/v1/economics/unit-economics",
            params={"business_id": str(_BUSINESS_ID), "product_id": str(_PRODUCT_ID)},
        )

        assert response.status_code == 200
        assert response.json()["contribution_margin"] == {"amount": 724.74, "currency": "EUR"}

    def test_returns_404_when_profile_missing(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/economics/unit-economics",
            params={"business_id": str(_BUSINESS_ID), "product_id": str(uuid.uuid4())},
        )

        assert response.status_code == 404

    async def test_target_cpa_route(
        self, client: TestClient, profiles: InMemoryUnitEconomicsProfileRepository
    ) -> None:
        await _seed_profile(profiles)

        response = client.get(
            "/api/v1/economics/target-cpa",
            params={"business_id": str(_BUSINESS_ID), "product_id": str(_PRODUCT_ID)},
        )

        assert response.status_code == 200
        assert response.json()["confidence"] == "confirmed"


class TestPlatformDivergenceRoute:
    def test_returns_404_without_snapshot(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/economics/platform-divergence",
            params={"business_id": str(_BUSINESS_ID), "platform_account_id": str(uuid.uuid4())},
        )

        assert response.status_code == 404
