"""Casos de uso de `optimization` contra los dobles en memoria de
`optimization/testing` (patron `application layer tested with in-memory
adapters`)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.application.diagnose_entity import DiagnoseEntity
from safent_ads.optimization.application.dto import StoredResponseCurve
from safent_ads.optimization.application.errors import (
    ContributionMarginNotFoundError,
    DiagnosisMetricsNotFoundError,
    MarginalEstimateNotFoundError,
    NoReallocationCandidatesError,
    ReallocationVetoedError,
    ResponseCurveNotFoundError,
)
from safent_ads.optimization.application.get_marginal_roas import GetMarginalRoas
from safent_ads.optimization.application.propose_reallocation_plan import ProposeReallocationPlan
from safent_ads.optimization.application.simulate_spend_change import SimulateSpendChange
from safent_ads.optimization.domain.allocation import AllocationCandidate
from safent_ads.optimization.domain.diagnosis import DiagnosisAction, EntityDiagnosisMetrics
from safent_ads.optimization.domain.marginal import EstimationMethod, MarginalEstimate
from safent_ads.optimization.domain.response_curve import (
    CurveConfidence,
    HillCurve,
    ObservedSpendRange,
)
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

_BUSINESS_ID = BusinessId.new()
_ENTITY_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")
_NOW = datetime(2026, 9, 9, tzinfo=UTC)


class TestGetMarginalRoas:
    async def test_returns_view_when_estimate_exists(self) -> None:
        repo = InMemoryMarginalEstimateRepository()
        estimate = MarginalEstimate(
            value=0.62, ci_low=0.1, ci_high=0.9, method=EstimationMethod.PAIRED, sample_size=7
        )
        repo.seed(business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF, estimate=estimate)
        use_case = GetMarginalRoas(repo)

        view = await use_case.execute(business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF)

        assert view.value == 0.62
        assert view.method == "paired"

    async def test_raises_when_missing(self) -> None:
        use_case = GetMarginalRoas(InMemoryMarginalEstimateRepository())
        with pytest.raises(MarginalEstimateNotFoundError):
            await use_case.execute(business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF)


def _clean_metrics() -> EntityDiagnosisMetrics:
    return EntityDiagnosisMetrics(
        unattributed_share=0.10,
        delta_hat=1.0,
        utm_valid=True,
        bridge_has_recent_events_24h=True,
        is_stale=False,
        is_suspended=False,
        is_drifted=False,
        is_learning=False,
        lost_is_budget_pct=0.05,
        lost_is_rank_pct=0.05,
        cpm_change_vs_14d_pct=0.0,
        ctr_7d_vs_median90d_ratio=1.0,
        quality_score_below_average=False,
        click_to_lead_vs_median90d_ratio=1.0,
        lead_to_business_conversion_vs_offering_median_ratio=1.0,
        frequency=1.5,
        is_retargeting_audience=False,
        ctr_declining=False,
        cpm_rising=False,
        cohort_maturity=0.9,
        projected_cpe_meets_target=False,
        within_seasonality_band=False,
    )


class TestDiagnoseEntity:
    async def test_returns_full_path_and_action(self) -> None:
        port = InMemoryDiagnosisMetricsPort()
        port.seed(business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF, metrics=_clean_metrics())
        use_case = DiagnoseEntity(port)

        view = await use_case.execute(business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF)

        assert view.action is DiagnosisAction.HOLD
        assert len(view.nodes) == 9

    async def test_raises_when_no_metrics(self) -> None:
        use_case = DiagnoseEntity(InMemoryDiagnosisMetricsPort())
        with pytest.raises(DiagnosisMetricsNotFoundError):
            await use_case.execute(business_id=_BUSINESS_ID, entity_ref=_ENTITY_REF)


class TestSimulateSpendChange:
    async def test_returns_contribution_delta_band(self) -> None:
        curves = InMemoryResponseCurveRepository()
        curves.seed(
            business_id=_BUSINESS_ID,
            entity_ref=_ENTITY_REF,
            record=StoredResponseCurve(
                curve=HillCurve(e_max=100.0, k=500.0),
                observed_range=ObservedSpendRange(min_spend=300.0, max_spend=900.0),
                residual_std=2.0,
                curve_confidence=CurveConfidence.OBSERVATIONAL,
            ),
        )
        margins = InMemoryContributionMarginPort()
        margins.seed(
            business_id=_BUSINESS_ID, product_id="offering-1", contribution_margin=Money.of("500")
        )
        use_case = SimulateSpendChange(curves, margins)

        view = await use_case.execute(
            business_id=_BUSINESS_ID,
            entity_ref=_ENTITY_REF,
            product_id="offering-1",
            current_daily_spend=Money.of("600"),
            spend_multiplier=1.2,
        )

        assert view.out_of_support is False
        assert view.expected_contribution_delta is not None

    async def test_out_of_support_returns_flag_without_numbers(self) -> None:
        curves = InMemoryResponseCurveRepository()
        curves.seed(
            business_id=_BUSINESS_ID,
            entity_ref=_ENTITY_REF,
            record=StoredResponseCurve(
                curve=HillCurve(e_max=100.0, k=500.0),
                observed_range=ObservedSpendRange(min_spend=300.0, max_spend=400.0),
                residual_std=2.0,
                curve_confidence=CurveConfidence.OBSERVATIONAL,
            ),
        )
        margins = InMemoryContributionMarginPort()
        margins.seed(
            business_id=_BUSINESS_ID, product_id="offering-1", contribution_margin=Money.of("500")
        )
        use_case = SimulateSpendChange(curves, margins)

        view = await use_case.execute(
            business_id=_BUSINESS_ID,
            entity_ref=_ENTITY_REF,
            product_id="offering-1",
            current_daily_spend=Money.of("350"),
            spend_multiplier=3.0,
        )

        assert view.out_of_support is True
        assert view.expected_contribution_delta is None

    async def test_raises_when_curve_missing(self) -> None:
        margins = InMemoryContributionMarginPort()
        use_case = SimulateSpendChange(InMemoryResponseCurveRepository(), margins)
        with pytest.raises(ResponseCurveNotFoundError):
            await use_case.execute(
                business_id=_BUSINESS_ID,
                entity_ref=_ENTITY_REF,
                product_id="offering-1",
                current_daily_spend=Money.of("600"),
                spend_multiplier=1.2,
            )

    async def test_raises_when_contribution_margin_missing(self) -> None:
        curves = InMemoryResponseCurveRepository()
        curves.seed(
            business_id=_BUSINESS_ID,
            entity_ref=_ENTITY_REF,
            record=StoredResponseCurve(
                curve=HillCurve(e_max=100.0, k=500.0),
                observed_range=ObservedSpendRange(min_spend=300.0, max_spend=900.0),
                residual_std=2.0,
                curve_confidence=CurveConfidence.OBSERVATIONAL,
            ),
        )
        use_case = SimulateSpendChange(curves, InMemoryContributionMarginPort())
        with pytest.raises(ContributionMarginNotFoundError):
            await use_case.execute(
                business_id=_BUSINESS_ID,
                entity_ref=_ENTITY_REF,
                product_id="offering-1",
                current_daily_spend=Money.of("600"),
                spend_multiplier=1.2,
            )


def _candidate(
    external_id: str, *, value: float, ci_low: float | None = None, ci_high: float | None = None
) -> AllocationCandidate:
    return AllocationCandidate(
        entity_ref=EntityRef(
            platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id=external_id
        ),
        current_daily_spend=Money.of("150"),
        min_viable_daily_spend=Money.of("50"),
        marginal_estimate=MarginalEstimate(
            value=value,
            ci_low=value - 1 if ci_low is None else ci_low,
            ci_high=value + 1 if ci_high is None else ci_high,
            method=EstimationMethod.PAIRED,
            sample_size=7,
        ),
        learning_status=LearningStatus.SUCCESS,
    )


class TestProposeReallocationPlan:
    async def test_raises_reallocation_proposals_and_returns_view(self) -> None:
        candidates_repo = InMemoryReallocationCandidateRepository()
        candidates_repo.seed(
            business_id=_BUSINESS_ID,
            candidates=[_candidate("low", value=0.5), _candidate("high", value=3.5)],
        )
        proposals_port = InMemoryReallocationProposalPort()
        use_case = ProposeReallocationPlan(candidates_repo, proposals_port, FixedClock(_NOW))

        view = await use_case.execute(business_id=_BUSINESS_ID)

        assert view.donor.direction == "decrease"
        assert view.receiver.direction == "increase"
        assert view.decrease_proposal_id
        assert view.increase_proposal_id
        assert len(proposals_port.raised_plans) == 1

    async def test_raises_when_no_candidates(self) -> None:
        use_case = ProposeReallocationPlan(
            InMemoryReallocationCandidateRepository(),
            InMemoryReallocationProposalPort(),
            FixedClock(_NOW),
        )
        with pytest.raises(NoReallocationCandidatesError):
            await use_case.execute(business_id=_BUSINESS_ID)

    async def test_raises_when_only_pair_is_vetoed(self) -> None:
        # IC solapado en sentido equivocado: donor.ci_low >= receiver.ci_high.
        candidates_repo = InMemoryReallocationCandidateRepository()
        candidates_repo.seed(
            business_id=_BUSINESS_ID,
            candidates=[
                _candidate("low", value=1.0, ci_low=3.0, ci_high=4.0),
                _candidate("high", value=2.0, ci_low=1.5, ci_high=2.5),
            ],
        )
        use_case = ProposeReallocationPlan(
            candidates_repo, InMemoryReallocationProposalPort(), FixedClock(_NOW)
        )
        with pytest.raises(ReallocationVetoedError):
            await use_case.execute(business_id=_BUSINESS_ID)
