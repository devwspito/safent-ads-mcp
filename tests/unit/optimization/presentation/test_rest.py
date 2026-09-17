"""Rutas REST de `optimization` (contracts/rest-api.md §Economia y
optimizacion)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from safent_ads.economics.domain.money import Money
from safent_ads.iam.presentation.dependencies import require_business_access
from safent_ads.optimization.application.query_service import OptimizationQueryService
from safent_ads.optimization.domain.allocation import AllocationCandidate
from safent_ads.optimization.domain.marginal import EstimationMethod, MarginalEstimate
from safent_ads.optimization.presentation.rest import build_optimization_router
from safent_ads.optimization.testing.in_memory_repositories import (
    InMemoryContributionMarginPort,
    InMemoryDiagnosisMetricsPort,
    InMemoryMarginalEstimateRepository,
    InMemoryReallocationCandidateRepository,
    InMemoryReallocationProposalPort,
    InMemoryResponseCurveRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from safent_ads.signals.domain.gates import LearningStatus

_BUSINESS_ID = uuid.uuid4()
_ENTITY_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")


async def _pass_through_business_access(business_id: uuid.UUID) -> uuid.UUID:
    return business_id


@pytest.fixture
def marginal_estimates() -> InMemoryMarginalEstimateRepository:
    return InMemoryMarginalEstimateRepository()


@pytest.fixture
def candidates() -> InMemoryReallocationCandidateRepository:
    return InMemoryReallocationCandidateRepository()


@pytest.fixture
def service(
    marginal_estimates: InMemoryMarginalEstimateRepository,
    candidates: InMemoryReallocationCandidateRepository,
) -> OptimizationQueryService:
    return OptimizationQueryService(
        marginal_estimates=marginal_estimates,
        diagnosis_metrics=InMemoryDiagnosisMetricsPort(),
        response_curves=InMemoryResponseCurveRepository(),
        contribution_margins=InMemoryContributionMarginPort(),
        reallocation_candidates=candidates,
        reallocation_proposals=InMemoryReallocationProposalPort(),
        clock=FixedClock(datetime(2026, 6, 1, tzinfo=UTC)),
    )


@pytest.fixture
def client(service: OptimizationQueryService) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(build_optimization_router(service))
    app.dependency_overrides[require_business_access] = _pass_through_business_access
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestMarginalRoasRoute:
    async def test_returns_view_when_estimate_exists(
        self, client: TestClient, marginal_estimates: InMemoryMarginalEstimateRepository
    ) -> None:
        marginal_estimates.seed(
            business_id=BusinessId(_BUSINESS_ID),
            entity_ref=_ENTITY_REF,
            estimate=MarginalEstimate(
                value=0.62, ci_low=0.1, ci_high=0.9, method=EstimationMethod.PAIRED, sample_size=7
            ),
        )

        response = client.get(
            "/api/v1/optimization/marginal-roas",
            params={"business_id": str(_BUSINESS_ID), "entity_ref": str(_ENTITY_REF)},
        )

        assert response.status_code == 200
        assert response.json()["value"] == pytest.approx(0.62)

    def test_returns_404_when_missing(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/optimization/marginal-roas",
            params={"business_id": str(_BUSINESS_ID), "entity_ref": str(_ENTITY_REF)},
        )

        assert response.status_code == 404

    def test_returns_422_on_malformed_entity_ref(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/optimization/marginal-roas",
            params={"business_id": str(_BUSINESS_ID), "entity_ref": "not-a-ref"},
        )

        assert response.status_code == 422


class TestDiagnosisRoute:
    def test_returns_404_without_metrics(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/optimization/diagnosis",
            params={"business_id": str(_BUSINESS_ID), "entity_ref": str(_ENTITY_REF)},
        )

        assert response.status_code == 404


class TestSpendSimulationRoute:
    def test_returns_404_without_curve(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/optimization/spend-simulation",
            params={
                "business_id": str(_BUSINESS_ID),
                "entity_ref": str(_ENTITY_REF),
                "product_id": str(uuid.uuid4()),
                "current_daily_spend_amount": "600",
                "spend_multiplier": 1.2,
            },
        )

        assert response.status_code == 404

    def test_returns_422_on_malformed_amount(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/optimization/spend-simulation",
            params={
                "business_id": str(_BUSINESS_ID),
                "entity_ref": str(_ENTITY_REF),
                "product_id": str(uuid.uuid4()),
                "current_daily_spend_amount": "not-a-number",
                "spend_multiplier": 1.2,
            },
        )

        assert response.status_code == 422


class TestReallocationPlanRoute:
    def test_returns_404_without_candidates(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/optimization/reallocation-plan", params={"business_id": str(_BUSINESS_ID)}
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "NO_CANDIDATES"

    def test_returns_201_with_two_linked_proposals(
        self, client: TestClient, candidates: InMemoryReallocationCandidateRepository
    ) -> None:
        candidates.seed(
            business_id=BusinessId(_BUSINESS_ID),
            candidates=[_candidate("low", value=0.5), _candidate("high", value=3.5)],
        )

        response = client.post(
            "/api/v1/optimization/reallocation-plan", params={"business_id": str(_BUSINESS_ID)}
        )

        assert response.status_code == 201
        body = response.json()
        assert body["decrease_proposal_id"]
        assert body["increase_proposal_id"]
        assert body["donor"]["requires_approval"] is False
        assert body["receiver"]["requires_approval"] is True


def _candidate(external_id: str, *, value: float) -> AllocationCandidate:
    return AllocationCandidate(
        entity_ref=EntityRef(
            platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id=external_id
        ),
        current_daily_spend=Money.of("150"),
        min_viable_daily_spend=Money.of("50"),
        marginal_estimate=MarginalEstimate(
            value=value,
            ci_low=value - 0.1,
            ci_high=value + 0.1,
            method=EstimationMethod.PAIRED,
            sample_size=7,
        ),
        learning_status=LearningStatus.SUCCESS,
    )
