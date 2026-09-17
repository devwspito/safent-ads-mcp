"""`DesignExperiment`/`ProposeExperiment`/`GetExperimentStatus`/
`GetCalibrationReport` (profitability-engine.md §4/§6, contracts/mcp-tools.md
P2, tasks.md T201) contra los dobles en memoria de `optimization/testing`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.optimization.application.design_experiment import (
    DesignExperiment,
    DesignExperimentRequest,
)
from safent_ads.optimization.application.errors import ExperimentNotFoundError
from safent_ads.optimization.application.get_calibration_report import GetCalibrationReport
from safent_ads.optimization.application.get_experiment_status import GetExperimentStatus
from safent_ads.optimization.application.propose_experiment import (
    ProposeExperiment,
    ProposeExperimentRequest,
)
from safent_ads.optimization.domain.calibration import OutcomeSource, SignalOutcome
from safent_ads.optimization.domain.errors import ImpossibleExperimentDesignError
from safent_ads.optimization.domain.identifiers import ExperimentId, SignalOutcomeId
from safent_ads.optimization.testing.in_memory_repositories import (
    InMemoryCalibrationInputPort,
    InMemoryExperimentProposalPort,
    InMemoryExperimentRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = BusinessId.new()
_ENTITY_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="c1")
_NOW = datetime(2026, 9, 9, tzinfo=UTC)


class TestDesignExperiment:
    def test_feasible_design_on_click_to_lead(self) -> None:
        # profitability-engine.md §4: "lead sobre clic... 2.274 clics/brazo,
        # alcanzable en 2-3 semanas".
        view = DesignExperiment().execute(
            DesignExperimentRequest(
                baseline_rate=0.08,
                relative_mde=0.25,
                available_units_per_arm_per_week=1200,
            )
        )

        assert view.feasible is True
        assert view.rejection_reason is None
        assert view.sample_per_arm > 0
        assert view.duration_days >= 7

    def test_rejects_a_design_below_the_available_volume(self) -> None:
        # profitability-engine.md §4: conversion de negocio sobre lead,
        # 6.560 leads/brazo -- inviable con los volumenes del propietario.
        view = DesignExperiment().execute(
            DesignExperimentRequest(
                baseline_rate=0.045,
                relative_mde=0.20,
                available_units_per_arm_per_week=50,
            )
        )

        assert view.feasible is False
        assert view.rejection_reason is not None
        assert view.duration_days == 0

    def test_duration_floor_applies_for_a_business_conversion_metric(self) -> None:
        view = DesignExperiment().execute(
            DesignExperimentRequest(
                baseline_rate=0.08,
                relative_mde=0.25,
                available_units_per_arm_per_week=1200,
                metric_measures_business_conversion=True,
                median_lag_days=30,
            )
        )

        assert view.minimum_duration_days == 37  # median_lag_days + 7


class TestProposeExperiment:
    async def _harness(
        self,
    ) -> tuple[ProposeExperiment, InMemoryExperimentProposalPort, InMemoryExperimentRepository]:
        experiments = InMemoryExperimentRepository()
        proposals = InMemoryExperimentProposalPort(experiments)
        use_case = ProposeExperiment(
            proposals=proposals, experiments=experiments, clock=FixedClock(_NOW)
        )
        return use_case, proposals, experiments

    async def test_creates_a_pending_proposal_and_a_draft_experiment(self) -> None:
        use_case, proposals, _ = await self._harness()

        view = await use_case.execute(
            ProposeExperimentRequest(
                business_id=_BUSINESS_ID,
                entity_ref=_ENTITY_REF,
                hypothesis="Meta generica supera a Google marca en CPL",
                metric="click_to_lead",
                baseline_rate=0.08,
                relative_mde=0.25,
                available_units_per_arm_per_week=1200,
            )
        )

        assert view.state == "draft"
        assert view.proposal_id == "proposal-1"
        assert len(proposals.raised) == 1
        raised_experiment = proposals.raised[0]["experiment"]
        assert raised_experiment.hypothesis == "Meta generica supera a Google marca en CPL"

    async def test_never_starts_the_experiment_by_itself(self) -> None:
        """§4: `propose_experiment` NUNCA corre autonomo -- crea la
        propuesta, no arranca el experimento."""
        use_case, _, _ = await self._harness()

        view = await use_case.execute(
            ProposeExperimentRequest(
                business_id=_BUSINESS_ID,
                entity_ref=_ENTITY_REF,
                hypothesis="h",
                metric="click_to_lead",
                baseline_rate=0.08,
                relative_mde=0.25,
                available_units_per_arm_per_week=1200,
            )
        )

        assert view.state == "draft"

    async def test_rejects_an_infeasible_design_before_creating_anything(self) -> None:
        use_case, proposals, experiments = await self._harness()

        with pytest.raises(ImpossibleExperimentDesignError):
            await use_case.execute(
                ProposeExperimentRequest(
                    business_id=_BUSINESS_ID,
                    entity_ref=_ENTITY_REF,
                    hypothesis="h",
                    metric="business_conversion_rate",
                    baseline_rate=0.045,
                    relative_mde=0.20,
                    available_units_per_arm_per_week=50,
                )
            )

        assert proposals.raised == []
        assert experiments._by_id == {}


class TestGetExperimentStatus:
    async def test_returns_the_stored_experiment(self) -> None:
        experiments = InMemoryExperimentRepository()
        use_case_propose = ProposeExperiment(
            proposals=InMemoryExperimentProposalPort(experiments),
            experiments=experiments,
            clock=FixedClock(_NOW),
        )
        created = await use_case_propose.execute(
            ProposeExperimentRequest(
                business_id=_BUSINESS_ID,
                entity_ref=_ENTITY_REF,
                hypothesis="h",
                metric="click_to_lead",
                baseline_rate=0.08,
                relative_mde=0.25,
                available_units_per_arm_per_week=1200,
            )
        )

        view = await GetExperimentStatus(experiments).execute(
            experiment_id=ExperimentId.parse(created.experiment_id)
        )

        assert view.hypothesis == "h"

    async def test_raises_when_unknown(self) -> None:
        with pytest.raises(ExperimentNotFoundError):
            await GetExperimentStatus(InMemoryExperimentRepository()).execute(
                experiment_id=ExperimentId.new()
            )


class TestGetCalibrationReport:
    async def test_reports_precision_per_rule(self) -> None:
        inputs = InMemoryCalibrationInputPort()
        inputs.seed(
            rule_code="G01",
            outcomes=[
                SignalOutcome(
                    outcome_id=SignalOutcomeId.new(),
                    business_id="biz-1",
                    account_id="acc-1",
                    rule_code="G01",
                    signal_id=f"sig-{i}",
                    outcome_source=OutcomeSource.EXPIRED,
                    was_correct=i < 5,
                    observed_at=_NOW,
                )
                for i in range(20)
            ],
        )

        report = await GetCalibrationReport(inputs).execute()

        assert len(report.rules) == 1
        assert report.rules[0].rule_code == "G01"
        assert report.rules[0].sample_size == 20
        assert report.rules[0].precision == pytest.approx(0.25)
        assert "0.25" in report.rules[0].recommendation
