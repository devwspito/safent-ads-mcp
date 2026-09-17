"""`build_equimarginal_plan` (profitability-engine.md §3): reproduce el
ejemplo trabajado (Secundaria Matematicas) -- A 150->120€/dia (AUTO), B
111->133€/dia (aprobacion), contribucion neta ~+56,6€/dia gastando 8€
menos. Tambien cubre los vetos: aprendizaje, IC solapado en sentido
equivocado, suelo `min_viable_spend` nunca perforado."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from safent_ads.economics.domain.money import Money
from safent_ads.optimization.domain.allocation import (
    AllocationCandidate,
    AllocationDirection,
    AllocationVetoReason,
    build_equimarginal_plan,
    enforce_daily_movement_cap,
)
from safent_ads.optimization.domain.identifiers import AllocationPlanId
from safent_ads.optimization.domain.marginal import EstimationMethod, MarginalEstimate
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from safent_ads.signals.domain.gates import LearningStatus

_DONOR_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.CAMPAIGN, external_id="A")
_RECEIVER_REF = EntityRef(platform=PlatformCode.META, level=EntityLevel.CAMPAIGN, external_id="B")
_NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _candidate(
    *,
    entity_ref: EntityRef,
    current: str,
    min_viable: str,
    value: float,
    ci_low: float,
    ci_high: float,
    learning: LearningStatus = LearningStatus.SUCCESS,
) -> AllocationCandidate:
    return AllocationCandidate(
        entity_ref=entity_ref,
        current_daily_spend=Money.of(current, "EUR"),
        min_viable_daily_spend=Money.of(min_viable, "EUR"),
        marginal_estimate=MarginalEstimate(
            value=value,
            ci_low=ci_low,
            ci_high=ci_high,
            method=EstimationMethod.PAIRED,
            sample_size=7,
        ),
        learning_status=learning,
    )


def _worked_example_donor() -> AllocationCandidate:
    return _candidate(
        entity_ref=_DONOR_REF, current="150", min_viable="50", value=0.62, ci_low=0.10, ci_high=0.90
    )


def _worked_example_receiver() -> AllocationCandidate:
    return _candidate(
        entity_ref=_RECEIVER_REF,
        current="111",
        min_viable="20",
        value=3.42,
        ci_low=2.0,
        ci_high=4.5,
    )


class TestWorkedExample:
    def test_donor_is_cut_by_20_percent_auto(self) -> None:
        verdict = build_equimarginal_plan(
            plan_id=AllocationPlanId.new(),
            business_id=BusinessId.new(),
            donor=_worked_example_donor(),
            receiver=_worked_example_receiver(),
            now=_NOW,
        )
        assert verdict.plan is not None
        donor_step = verdict.plan.donor_step
        assert donor_step.direction is AllocationDirection.DECREASE
        assert float(donor_step.proposed_daily_spend.amount) == pytest.approx(120.0, abs=1.0)
        assert donor_step.requires_approval is False

    def test_receiver_is_raised_approximately_20_percent_needs_approval(self) -> None:
        verdict = build_equimarginal_plan(
            plan_id=AllocationPlanId.new(),
            business_id=BusinessId.new(),
            donor=_worked_example_donor(),
            receiver=_worked_example_receiver(),
            now=_NOW,
        )
        assert verdict.plan is not None
        receiver_step = verdict.plan.receiver_step
        assert receiver_step.direction is AllocationDirection.INCREASE
        assert float(receiver_step.proposed_daily_spend.amount) == pytest.approx(133.0, abs=1.0)
        assert receiver_step.requires_approval is True

    def test_net_contribution_gain_matches_design_example(self) -> None:
        verdict = build_equimarginal_plan(
            plan_id=AllocationPlanId.new(),
            business_id=BusinessId.new(),
            donor=_worked_example_donor(),
            receiver=_worked_example_receiver(),
            now=_NOW,
        )
        assert verdict.plan is not None
        # profitability-engine.md §3: "Neto +56,6€/dia gastando 8€ menos".
        assert float(verdict.plan.expected_contribution_delta.amount) == pytest.approx(
            56.6, abs=2.0
        )
        total_before = (
            verdict.plan.donor_step.current_daily_spend.amount
            + verdict.plan.receiver_step.current_daily_spend.amount
        )
        total_after = (
            verdict.plan.donor_step.proposed_daily_spend.amount
            + verdict.plan.receiver_step.proposed_daily_spend.amount
        )
        assert float(total_before - total_after) == pytest.approx(8.0, abs=1.5)


class TestVetoes:
    def test_learning_entity_is_vetoed(self) -> None:
        donor = _candidate(
            entity_ref=_DONOR_REF,
            current="150",
            min_viable="50",
            value=0.62,
            ci_low=0.10,
            ci_high=0.90,
            learning=LearningStatus.LEARNING,
        )
        verdict = build_equimarginal_plan(
            plan_id=AllocationPlanId.new(),
            business_id=BusinessId.new(),
            donor=donor,
            receiver=_worked_example_receiver(),
            now=_NOW,
        )
        assert verdict.plan is None
        assert verdict.veto_reason is AllocationVetoReason.LEARNING

    def test_overlapping_confidence_intervals_are_vetoed(self) -> None:
        donor = _candidate(
            entity_ref=_DONOR_REF,
            current="150",
            min_viable="50",
            value=0.62,
            ci_low=3.0,
            ci_high=4.0,
        )
        receiver = _candidate(
            entity_ref=_RECEIVER_REF,
            current="111",
            min_viable="20",
            value=3.42,
            ci_low=2.0,
            ci_high=2.5,
        )
        verdict = build_equimarginal_plan(
            plan_id=AllocationPlanId.new(),
            business_id=BusinessId.new(),
            donor=donor,
            receiver=receiver,
            now=_NOW,
        )
        assert verdict.plan is None
        assert verdict.veto_reason is AllocationVetoReason.CONFIDENCE_INTERVALS_OVERLAP_WRONG_WAY

    def test_already_equimarginal_is_vetoed(self) -> None:
        donor = _candidate(
            entity_ref=_DONOR_REF,
            current="150",
            min_viable="50",
            value=3.42,
            ci_low=2.0,
            ci_high=4.5,
        )
        receiver = _candidate(
            entity_ref=_RECEIVER_REF,
            current="111",
            min_viable="20",
            value=3.42,
            ci_low=2.0,
            ci_high=4.5,
        )
        verdict = build_equimarginal_plan(
            plan_id=AllocationPlanId.new(),
            business_id=BusinessId.new(),
            donor=donor,
            receiver=receiver,
            now=_NOW,
        )
        assert verdict.plan is None
        assert verdict.veto_reason is AllocationVetoReason.ALREADY_EQUIMARGINAL


class TestMinViableSpendFloor:
    def test_donor_never_drops_below_min_viable_spend(self) -> None:
        donor = _candidate(
            entity_ref=_DONOR_REF,
            current="150",
            min_viable="145",
            value=0.62,
            ci_low=0.10,
            ci_high=0.90,
        )
        verdict = build_equimarginal_plan(
            plan_id=AllocationPlanId.new(),
            business_id=BusinessId.new(),
            donor=donor,
            receiver=_worked_example_receiver(),
            now=_NOW,
        )
        assert verdict.plan is not None
        assert verdict.plan.donor_step.proposed_daily_spend.amount >= 145


class TestGuardrailClamp:
    def test_guardrail_ceiling_caps_receiver_increase(self) -> None:
        tight_guardrail = GuardrailPolicy(
            daily_cap_minor=12_500,
            monthly_cap_minor=1_000_000,
            floor_minor=0,
            ceiling_minor=12_500,
            max_step_pct=100.0,
            max_changes_per_day=10,
        )
        verdict = build_equimarginal_plan(
            plan_id=AllocationPlanId.new(),
            business_id=BusinessId.new(),
            donor=_worked_example_donor(),
            receiver=_worked_example_receiver(),
            now=_NOW,
            receiver_guardrail=tight_guardrail,
        )
        assert verdict.plan is not None
        assert verdict.plan.receiver_step.proposed_daily_spend.amount <= 125


class TestDailyMovementCap:
    def test_scales_down_when_total_movement_exceeds_cap(self) -> None:
        verdict = build_equimarginal_plan(
            plan_id=AllocationPlanId.new(),
            business_id=BusinessId.new(),
            donor=_worked_example_donor(),
            receiver=_worked_example_receiver(),
            now=_NOW,
        )
        assert verdict.plan is not None
        steps = (verdict.plan.donor_step, verdict.plan.receiver_step)
        # Cartera pequeña: 15% de 261€ (150+111) = 39,15€ < movimiento sin capar (~52€).
        capped = enforce_daily_movement_cap(
            steps, portfolio_daily_spend=Money.of("261", "EUR"), cap_pct=0.15
        )
        total_movement = sum((abs(step.delta.amount) for step in capped), Decimal("0"))
        cap = Money.of("261", "EUR").amount * Decimal("0.15")
        assert total_movement <= cap + Decimal("1")
