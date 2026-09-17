"""`build_optimization_tool_specs` (profitability-engine.md §8 P1): verbo
primero salvo `propose_reallocation_plan` (verb_kind="proposal", nunca
escribe en plataforma), capacidad reportada nunca fingida (contracts/
mcp-tools.md reglas 2, 3, 6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.application.query_service import OptimizationQueryService
from safent_ads.optimization.domain.allocation import AllocationCandidate
from safent_ads.optimization.domain.marginal import EstimationMethod, MarginalEstimate
from safent_ads.optimization.presentation.mcp_tools import build_optimization_tool_specs
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

_READ_PREFIXES = ("list_", "get_", "search_", "run_", "explain_", "diagnose_", "simulate_")
_PROPOSAL_PREFIXES = ("propose_",)
_FORBIDDEN_DECISION_PREFIXES = ("approve_", "execute_", "apply_")
_BUSINESS_ID = "11111111-1111-1111-1111-111111111111"
_ENTITY_REF = "google:campaign:1234567890"


@pytest.fixture
def specs() -> list:
    service = OptimizationQueryService(
        marginal_estimates=InMemoryMarginalEstimateRepository(),
        diagnosis_metrics=InMemoryDiagnosisMetricsPort(),
        response_curves=InMemoryResponseCurveRepository(),
        contribution_margins=InMemoryContributionMarginPort(),
        reallocation_candidates=InMemoryReallocationCandidateRepository(),
        reallocation_proposals=InMemoryReallocationProposalPort(),
        clock=FixedClock(datetime(2026, 6, 1, tzinfo=UTC)),
    )
    return build_optimization_tool_specs(service)


def test_exposes_the_four_p1_tools(specs: list) -> None:
    names = {spec.name for spec in specs}
    assert names == {
        "get_marginal_roas",
        "diagnose_entity",
        "simulate_spend_change",
        "propose_reallocation_plan",
    }


def test_names_are_verb_first_and_never_approve_or_execute(specs: list) -> None:
    for spec in specs:
        assert spec.name.startswith((*_READ_PREFIXES, *_PROPOSAL_PREFIXES))
        assert not spec.name.startswith(_FORBIDDEN_DECISION_PREFIXES)


def test_only_propose_reallocation_plan_is_a_proposal(specs: list) -> None:
    kinds = {spec.name: spec.verb_kind for spec in specs}
    assert kinds["propose_reallocation_plan"] == "proposal"
    for name in ("get_marginal_roas", "diagnose_entity", "simulate_spend_change"):
        assert kinds[name] == "read"


class TestGetMarginalRoasHandler:
    async def test_reports_not_found_capability_instead_of_raising(self, specs: list) -> None:
        spec = next(s for s in specs if s.name == "get_marginal_roas")
        args = spec.args_model(business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF)

        result = await spec.handler(args)

        assert result["error"]["code"] == "ENTITY_NOT_FOUND"

    def test_rejects_malformed_entity_ref(self, specs: list) -> None:
        spec = next(s for s in specs if s.name == "get_marginal_roas")
        with pytest.raises(Exception, match="entity_ref|validation"):
            spec.args_model(business_id=_BUSINESS_ID, entity_ref="not-a-valid-ref")

    def test_rejects_extra_fields(self, specs: list) -> None:
        spec = next(s for s in specs if s.name == "get_marginal_roas")
        with pytest.raises(Exception, match="extra"):
            spec.args_model(business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF, extra="nope")


class TestDiagnoseEntityHandler:
    async def test_reports_not_found_capability_instead_of_raising(self, specs: list) -> None:
        spec = next(s for s in specs if s.name == "diagnose_entity")
        args = spec.args_model(business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF)

        result = await spec.handler(args)

        assert result["error"]["code"] == "ENTITY_NOT_FOUND"


class TestSimulateSpendChangeHandler:
    async def test_reports_not_found_when_curve_missing(self, specs: list) -> None:
        spec = next(s for s in specs if s.name == "simulate_spend_change")
        args = spec.args_model(
            business_id=_BUSINESS_ID,
            entity_ref=_ENTITY_REF,
            product_id="22222222-2222-2222-2222-222222222222",
            current_daily_spend_amount="600",
            spend_multiplier=1.2,
        )

        result = await spec.handler(args)

        assert result["error"]["code"] == "ENTITY_NOT_FOUND"

    async def test_rejects_malformed_amount_without_raising(self, specs: list) -> None:
        spec = next(s for s in specs if s.name == "simulate_spend_change")
        args = spec.args_model(
            business_id=_BUSINESS_ID,
            entity_ref=_ENTITY_REF,
            product_id="22222222-2222-2222-2222-222222222222",
            current_daily_spend_amount="not-a-number",
            spend_multiplier=1.2,
        )

        result = await spec.handler(args)

        assert result["error"]["code"] == "INVALID_ARGUMENT"


class TestProposeReallocationPlanHandler:
    async def test_reports_not_found_when_no_candidates(self, specs: list) -> None:
        spec = next(s for s in specs if s.name == "propose_reallocation_plan")
        args = spec.args_model(business_id=_BUSINESS_ID)

        result = await spec.handler(args)

        assert result["error"]["code"] == "ENTITY_NOT_FOUND"

    async def test_returns_plan_when_candidates_diverge_enough(self) -> None:
        candidates_repo = InMemoryReallocationCandidateRepository()
        business_id = BusinessId.parse(_BUSINESS_ID)
        candidates_repo.seed(
            business_id=business_id,
            candidates=[
                _candidate("low", value=0.5),
                _candidate("high", value=3.5),
            ],
        )
        service = OptimizationQueryService(
            marginal_estimates=InMemoryMarginalEstimateRepository(),
            diagnosis_metrics=InMemoryDiagnosisMetricsPort(),
            response_curves=InMemoryResponseCurveRepository(),
            contribution_margins=InMemoryContributionMarginPort(),
            reallocation_candidates=candidates_repo,
            reallocation_proposals=InMemoryReallocationProposalPort(),
            clock=FixedClock(datetime(2026, 6, 1, tzinfo=UTC)),
        )
        spec = next(
            s
            for s in build_optimization_tool_specs(service)
            if s.name == "propose_reallocation_plan"
        )
        args = spec.args_model(business_id=_BUSINESS_ID)

        result = await spec.handler(args)

        assert "error" not in result
        assert result["donor"]["direction"] == "decrease"
        assert result["receiver"]["direction"] == "increase"


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
