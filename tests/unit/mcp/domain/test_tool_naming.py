"""Regla pura verbo-primero (contracts/mcp-tools.md regla 2, INV-2)."""

from __future__ import annotations

import pytest

from safent_ads.mcp.domain.tool_naming import (
    is_forbidden_decision_verb,
    is_proposal_verb,
    is_read_verb,
)


@pytest.mark.parametrize(
    "name",
    [
        "list_signals",
        "get_rule",
        "search_decision_log",
        "run_gaql",
        "explain_rule",
        "diagnose_entity",
        "simulate_spend_change",
        "list_offerings",
        "list_calendar_events",
        "get_calendar_event",
        "design_experiment",
        "build_tracking_template",
        "validate_utm_consistency",
        "compare_attribution_windows",
    ],
)
def test_read_verbs_recognized(name: str) -> None:
    assert is_read_verb(name) is True


@pytest.mark.parametrize("name", ["approve_proposal", "execute_action", "apply_proposal_now"])
def test_forbidden_decision_verbs_recognized(name: str) -> None:
    assert is_forbidden_decision_verb(name) is True


@pytest.mark.parametrize(
    "name", ["propose_budget_change", "withdraw_proposal", "generate_creative_assets"]
)
def test_proposal_verbs_recognized(name: str) -> None:
    assert is_proposal_verb(name) is True


def test_read_verb_rejects_proposal_and_forbidden_names() -> None:
    assert is_read_verb("propose_budget_change") is False
    assert is_read_verb("approve_proposal") is False
