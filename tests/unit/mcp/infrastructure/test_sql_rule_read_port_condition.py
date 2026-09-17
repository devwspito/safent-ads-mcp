"""`_render_condition` (mcp/infrastructure/sql_rule_read_port.py): serializa
una `Condition` de `rules.domain` al `str` que pide `RuleDetail.condition`.
Puro, sin DB."""

from __future__ import annotations

from safent_ads.mcp.infrastructure.sql_rule_read_port import _render_condition
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind


def test_single_clause_renders_metric_comparator_and_value() -> None:
    condition = Condition(
        clauses=(
            ConditionClause(
                metric="cpl",
                comparator=Comparator.GTE,
                window="7D",
                threshold_kind=ThresholdKind.TARGET_RELATIVE_PCT,
                value=130.0,
            ),
        )
    )

    rendered = _render_condition(condition)

    assert rendered == "cpl gte 130.0 (target_relative_pct, 7D)"


def test_multiple_clauses_are_joined_with_and() -> None:
    condition = Condition(
        clauses=(
            ConditionClause(
                metric="cpl",
                comparator=Comparator.GTE,
                window="7D",
                threshold_kind=ThresholdKind.ABSOLUTE,
                value=30.0,
            ),
            ConditionClause(
                metric="spend",
                comparator=Comparator.GT,
                window="3D",
                threshold_kind=ThresholdKind.ABSOLUTE,
                value=100.0,
            ),
        )
    )

    rendered = _render_condition(condition)

    assert rendered == "cpl gte 30.0 (absolute, 7D) AND spend gt 100.0 (absolute, 3D)"
