"""Catalogo de acciones defensivas + precheck de `apply_defensive_action`
(T069; contracts/mcp-tools.md)."""

from __future__ import annotations

from safent_ads.execution.domain.defensive_actions import (
    DefensiveAction,
    DefensiveActionDenialCode,
    DefensiveActionKind,
    precheck_apply_defensive_action,
)
from safent_ads.execution.domain.guardrails import GuardrailVerdict
from safent_ads.proposals.domain.money import Money

_OK_VERDICT = GuardrailVerdict(
    allowed=True, clamped_after=Money.of("70"), reasons=(), verdict_hash="h"
)
_BLOCKED_VERDICT = GuardrailVerdict(
    allowed=False, clamped_after=Money.of("100"), reasons=("daily_cap_exceeded",), verdict_hash="h"
)


class TestIsDefensiveProof:
    def test_lower_budget_is_defensive(self) -> None:
        action = DefensiveAction(DefensiveActionKind.LOWER_BUDGET, magnitude_pct=0.30)

        assert action.is_defensive(Money.of("100"), Money.of("70")) is True

    def test_pause_is_defensive(self) -> None:
        action = DefensiveAction(DefensiveActionKind.PAUSE)

        assert action.is_defensive(Money.of("100"), Money.zero()) is True

    def test_add_negative_keyword_with_unchanged_budget_is_defensive(self) -> None:
        action = DefensiveAction(DefensiveActionKind.ADD_NEGATIVE_KEYWORD)

        assert action.is_defensive(Money.of("100"), Money.of("100")) is True

    def test_any_spend_increase_is_never_defensive(self) -> None:
        action = DefensiveAction(DefensiveActionKind.RESUME_ON_LATE_CONVERSION)

        assert action.is_defensive(Money.of("70"), Money.of("100")) is False


class TestLowerBudgetCatalogCeiling:
    def test_within_30_percent_respects_ceiling(self) -> None:
        action = DefensiveAction(DefensiveActionKind.LOWER_BUDGET, magnitude_pct=0.30)

        assert action.respects_catalog_ceiling() is True

    def test_above_30_percent_violates_ceiling(self) -> None:
        action = DefensiveAction(DefensiveActionKind.LOWER_BUDGET, magnitude_pct=0.50)

        assert action.respects_catalog_ceiling() is False

    def test_missing_magnitude_violates_ceiling(self) -> None:
        action = DefensiveAction(DefensiveActionKind.LOWER_BUDGET, magnitude_pct=None)

        assert action.respects_catalog_ceiling() is False

    def test_non_budget_actions_have_no_ceiling(self) -> None:
        action = DefensiveAction(DefensiveActionKind.PAUSE)

        assert action.respects_catalog_ceiling() is True


class TestPrecheckPrecedence:
    _DEFENSIVE = DefensiveAction(DefensiveActionKind.LOWER_BUDGET, magnitude_pct=0.30)

    def test_brake_engaged_wins_over_everything_else(self) -> None:
        code = precheck_apply_defensive_action(
            self._DEFENSIVE,
            Money.of("100"),
            Money.of("70"),
            brake_engaged=True,
            rule_condition_is_live=False,
            data_is_stale=True,
            guardrail_verdict=_BLOCKED_VERDICT,
        )

        assert code is DefensiveActionDenialCode.BRAKE_ENGAGED

    def test_stale_data_before_rule_liveness(self) -> None:
        code = precheck_apply_defensive_action(
            self._DEFENSIVE,
            Money.of("100"),
            Money.of("70"),
            brake_engaged=False,
            rule_condition_is_live=False,
            data_is_stale=True,
            guardrail_verdict=None,
        )

        assert code is DefensiveActionDenialCode.STALE_DATA

    def test_rule_not_applicable_when_condition_not_live(self) -> None:
        code = precheck_apply_defensive_action(
            self._DEFENSIVE,
            Money.of("100"),
            Money.of("70"),
            brake_engaged=False,
            rule_condition_is_live=False,
            data_is_stale=False,
            guardrail_verdict=None,
        )

        assert code is DefensiveActionDenialCode.RULE_NOT_APPLICABLE

    def test_validation_error_when_action_is_not_defensive(self) -> None:
        code = precheck_apply_defensive_action(
            DefensiveAction(DefensiveActionKind.RESUME_ON_LATE_CONVERSION),
            Money.of("70"),
            Money.of("100"),
            brake_engaged=False,
            rule_condition_is_live=True,
            data_is_stale=False,
            guardrail_verdict=None,
        )

        assert code is DefensiveActionDenialCode.VALIDATION_ERROR

    def test_validation_error_when_magnitude_exceeds_catalog_ceiling(self) -> None:
        code = precheck_apply_defensive_action(
            DefensiveAction(DefensiveActionKind.LOWER_BUDGET, magnitude_pct=0.50),
            Money.of("100"),
            Money.of("50"),
            brake_engaged=False,
            rule_condition_is_live=True,
            data_is_stale=False,
            guardrail_verdict=None,
        )

        assert code is DefensiveActionDenialCode.VALIDATION_ERROR

    def test_guardrail_blocked_when_verdict_denies(self) -> None:
        code = precheck_apply_defensive_action(
            self._DEFENSIVE,
            Money.of("100"),
            Money.of("70"),
            brake_engaged=False,
            rule_condition_is_live=True,
            data_is_stale=False,
            guardrail_verdict=_BLOCKED_VERDICT,
        )

        assert code is DefensiveActionDenialCode.GUARDRAIL_BLOCKED

    def test_none_returned_when_everything_passes(self) -> None:
        code = precheck_apply_defensive_action(
            self._DEFENSIVE,
            Money.of("100"),
            Money.of("70"),
            brake_engaged=False,
            rule_condition_is_live=True,
            data_is_stale=False,
            guardrail_verdict=_OK_VERDICT,
        )

        assert code is None
