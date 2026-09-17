"""`GuardrailSet` composicion + `GuardrailEvaluator` (T059/T060)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from safent_ads.execution.domain.guardrails import (
    GuardrailChange,
    GuardrailEvaluator,
    GuardrailInvariantError,
    GuardrailRelaxationError,
    LedgerSnapshot,
)
from safent_ads.proposals.domain.authorization import AuthorizationKind
from safent_ads.proposals.domain.money import Money

from .conftest import account_scope, entity_ref, entity_scope, guardrail_set

_EVALUATOR = GuardrailEvaluator()


@pytest.mark.parametrize("reserved,settled", [("20", "0"), ("0", "20")])
def test_monthly_commitment_survives_reservation_settlement(reserved: str, settled: str) -> None:
    ledger = replace(
        _empty_ledger(),
        reserved_increase=Money.of(reserved),
        applied_increases_month_to_date=Money.of(settled),
    )
    change = GuardrailChange(
        scope=account_scope(),
        entity_ref=entity_ref(),
        authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
        before=Money.of("70"),
        after=Money.of("90"),
    )
    verdict = _EVALUATOR.evaluate(change, guardrail_set(monthly_cap="20", max_step_pct=1.0), ledger)
    assert not verdict.allowed
    assert "monthly_cap_exceeded" in verdict.reasons


def _empty_ledger(
    spend_today: str = "0", spend_mtd: str = "0", applied_today: str = "0", changes_today: int = 0
) -> LedgerSnapshot:
    return LedgerSnapshot(
        platform_spend_today=Money.of(spend_today),
        platform_spend_month_to_date=Money.of(spend_mtd),
        applied_changes_today=Money.of(applied_today),
        changes_count_today_for_entity=changes_today,
    )


class TestSpecificScopeCannotRelaxGeneral:
    def test_specific_scope_cannot_relax_general(self) -> None:
        general = guardrail_set(scope=account_scope(), daily_cap="500", floor="10", ceiling="300")
        relaxed_specific = guardrail_set(
            scope=entity_scope(), daily_cap="600", floor="10", ceiling="300"
        )

        with pytest.raises(GuardrailRelaxationError):
            relaxed_specific.effective_with(general)

    def test_specific_scope_may_tighten_general(self) -> None:
        general = guardrail_set(scope=account_scope(), daily_cap="500", floor="10", ceiling="300")
        stricter_specific = guardrail_set(
            scope=entity_scope(), daily_cap="200", floor="20", ceiling="150"
        )

        effective = stricter_specific.effective_with(general)

        assert effective is stricter_specific

    def test_relaxed_floor_is_rejected(self) -> None:
        general = guardrail_set(floor="20")
        relaxed = guardrail_set(floor="10")

        with pytest.raises(GuardrailRelaxationError):
            relaxed.effective_with(general)

    def test_relaxed_max_step_pct_is_rejected(self) -> None:
        general = guardrail_set(max_step_pct=0.10)
        relaxed = guardrail_set(max_step_pct=0.30)

        with pytest.raises(GuardrailRelaxationError):
            relaxed.effective_with(general)

    def test_relaxed_max_changes_per_day_is_rejected(self) -> None:
        general = guardrail_set(max_changes_per_entity_day=1)
        relaxed = guardrail_set(max_changes_per_entity_day=5)

        with pytest.raises(GuardrailRelaxationError):
            relaxed.effective_with(general)

    def test_floor_above_ceiling_is_invalid(self) -> None:
        with pytest.raises(GuardrailInvariantError):
            guardrail_set(floor="300", ceiling="10")


class TestAutoActionRejectsSpendIncrease:
    def test_auto_action_rejects_spend_increase(self) -> None:
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.RULE_AUTHORIZATION,
            before=Money.of("100"),
            after=Money.of("120"),
        )

        verdict = _EVALUATOR.evaluate(change, guardrail_set(), _empty_ledger())

        assert verdict.allowed is False
        assert "auto_action_would_increase_spend" in verdict.reasons

    def test_auto_action_decrease_is_allowed(self) -> None:
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.RULE_AUTHORIZATION,
            before=Money.of("100"),
            after=Money.of("80"),
        )

        verdict = _EVALUATOR.evaluate(change, guardrail_set(), _empty_ledger())

        assert verdict.allowed is True
        assert verdict.clamped_after == Money.of("80")

    def test_auto_decrease_clamped_above_the_live_value_is_rejected(self) -> None:
        # Bajada nominal (100 -> 20) con suelo 150: el clamp la convertiria en
        # una SUBIDA real (100 -> 150). FR-11 se aplica al valor efectivo.
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.RULE_AUTHORIZATION,
            before=Money.of("100"),
            after=Money.of("20"),
        )

        verdict = _EVALUATOR.evaluate(
            change, guardrail_set(floor="150", ceiling="500", max_step_pct=1.0), _empty_ledger()
        )

        assert verdict.allowed is False
        assert verdict.clamped_after == Money.of("100")
        assert "auto_action_would_increase_spend_after_clamp" in verdict.reasons

    def test_human_approval_clamped_above_the_live_value_is_allowed(self) -> None:
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=Money.of("100"),
            after=Money.of("20"),
        )

        verdict = _EVALUATOR.evaluate(
            change, guardrail_set(floor="150", ceiling="500", max_step_pct=1.0), _empty_ledger()
        )

        assert verdict.allowed is True
        assert verdict.clamped_after == Money.of("150")
        assert "clamped_to_floor" in verdict.reasons

    def test_human_approval_may_increase_spend_within_caps(self) -> None:
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=Money.of("100"),
            after=Money.of("110"),
        )

        verdict = _EVALUATOR.evaluate(change, guardrail_set(max_step_pct=0.30), _empty_ledger())

        assert verdict.allowed is True


class TestFloorAndCeilingClamp:
    def test_below_floor_is_clamped_and_still_allowed(self) -> None:
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.RULE_AUTHORIZATION,
            before=Money.of("20"),
            after=Money.of("5"),
        )

        verdict = _EVALUATOR.evaluate(
            change, guardrail_set(floor="10", max_step_pct=1.0), _empty_ledger()
        )

        assert verdict.allowed is True
        assert verdict.clamped_after == Money.of("10")
        assert "clamped_to_floor" in verdict.reasons

    def test_above_ceiling_is_clamped(self) -> None:
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=Money.of("250"),
            after=Money.of("400"),
        )

        verdict = _EVALUATOR.evaluate(
            change, guardrail_set(ceiling="300", max_step_pct=1.0), _empty_ledger()
        )

        assert verdict.clamped_after == Money.of("300")
        assert "clamped_to_ceiling" in verdict.reasons


class TestMaxStepClamp:
    def test_step_above_max_pct_is_clamped(self) -> None:
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.RULE_AUTHORIZATION,
            before=Money.of("100"),
            after=Money.of("50"),
        )

        verdict = _EVALUATOR.evaluate(
            change, guardrail_set(max_step_pct=0.30, floor="0"), _empty_ledger()
        )

        assert verdict.clamped_after == Money.of("70.00")
        assert "clamped_to_max_step" in verdict.reasons


class TestMaxChangesPerEntityDay:
    def test_defers_when_max_changes_already_reached(self) -> None:
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.RULE_AUTHORIZATION,
            before=Money.of("100"),
            after=Money.of("80"),
        )
        ledger = _empty_ledger(changes_today=2)

        verdict = _EVALUATOR.evaluate(change, guardrail_set(max_changes_per_entity_day=2), ledger)

        assert verdict.allowed is False
        assert "max_changes_per_entity_day_reached" in verdict.reasons


class TestManySmallChangesHitDailyCap:
    def test_many_small_changes_hit_daily_cap(self) -> None:
        """C-17: el tope se mide sobre gasto reportado + cambios ya
        aplicados hoy, no por cambio aislado — evita el "goteo" (threat-model
        C-16/C-17: N cambios bajo el salto maximo cuya suma revienta el
        tope)."""
        guardrails = guardrail_set(daily_cap="500", max_step_pct=1.0, max_changes_per_entity_day=99)
        ledger_after_many_small_increases = _empty_ledger(
            spend_today="480", applied_today="15", changes_today=10
        )
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=Money.of("50"),
            after=Money.of("60"),  # +10, deja el proyectado en 480+15+10=505 > 500
        )

        verdict = _EVALUATOR.evaluate(change, guardrails, ledger_after_many_small_increases)

        assert verdict.allowed is False
        assert "daily_cap_exceeded" in verdict.reasons

    def test_stays_under_daily_cap_is_allowed(self) -> None:
        guardrails = guardrail_set(daily_cap="500", max_step_pct=1.0)
        ledger = _empty_ledger(spend_today="400", applied_today="0")
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=Money.of("50"),
            after=Money.of("60"),
        )

        verdict = _EVALUATOR.evaluate(change, guardrails, ledger)

        assert verdict.allowed is True

    def test_monthly_cap_also_blocks(self) -> None:
        guardrails = guardrail_set(monthly_cap="10000", max_step_pct=1.0)
        ledger = _empty_ledger(spend_mtd="9995")
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=Money.of("50"),
            after=Money.of("60"),
        )

        verdict = _EVALUATOR.evaluate(change, guardrails, ledger)

        assert verdict.allowed is False
        assert "monthly_cap_exceeded" in verdict.reasons


class TestVerdictHashIsDeterministicAndBindable:
    def test_same_inputs_produce_same_verdict_hash(self) -> None:
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=Money.of("100"),
            after=Money.of("110"),
        )
        guardrails = guardrail_set()
        ledger = _empty_ledger()

        first = _EVALUATOR.evaluate(change, guardrails, ledger)
        second = _EVALUATOR.evaluate(change, guardrails, ledger)

        assert first.verdict_hash == second.verdict_hash

    def test_different_outcome_changes_verdict_hash(self) -> None:
        guardrails = guardrail_set(daily_cap="500", max_step_pct=1.0)
        change = GuardrailChange(
            scope=account_scope(),
            entity_ref=entity_ref(),
            authorization_kind=AuthorizationKind.HUMAN_APPROVAL,
            before=Money.of("50"),
            after=Money.of("60"),
        )

        allowed_verdict = _EVALUATOR.evaluate(change, guardrails, _empty_ledger(spend_today="0"))
        blocked_verdict = _EVALUATOR.evaluate(change, guardrails, _empty_ledger(spend_today="495"))

        assert allowed_verdict.verdict_hash != blocked_verdict.verdict_hash
