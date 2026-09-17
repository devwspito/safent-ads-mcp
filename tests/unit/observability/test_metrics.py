"""T127: nombres/etiquetas de `observability.metrics`. Cardinalidad
acotada (tasks.md T127: "no high-cardinality labels: never entity ids,
never business ids beyond a bounded count") -- toda etiqueta que este
modulo expone es un valor de un enum de dominio cerrado o una de las dos
plataformas soportadas, nunca un identificador."""

from __future__ import annotations

from safent_ads.observability import metrics

_EXPECTED_METRIC_NAMES = (
    "ads_orchestration_cycle_duration_seconds",
    "ads_orchestration_cycles_total",
    "ads_orchestration_steps_total",
    "ads_execution_outcomes_total",
    "ads_proposals_saved_total",
    "ads_broker_write_denials_total",
    "ads_credential_health",
    "ads_mcp_tool_calls_total",
    "ads_mcp_tool_call_duration_seconds",
)


def test_every_expected_metric_name_is_rendered() -> None:
    rendered = metrics.render_latest().decode("utf-8")
    missing = [name for name in _EXPECTED_METRIC_NAMES if name not in rendered]
    assert missing == []


def test_cycle_and_step_labels_are_bounded_enum_values_not_ids() -> None:
    assert metrics.cycle_duration_seconds._labelnames == ("cycle",)
    assert metrics.cycles_total._labelnames == ("cycle", "outcome")
    assert metrics.steps_total._labelnames == ("step", "outcome")
    assert metrics.execution_outcomes_total._labelnames == ("outcome",)
    assert metrics.proposals_saved_total._labelnames == ("state",)
    assert metrics.broker_write_denials_total._labelnames == ("control",)
    assert metrics.credential_health._labelnames == ("platform", "status")
    assert metrics.mcp_tool_calls_total._labelnames == ("tool", "outcome")
    assert metrics.mcp_tool_call_duration_seconds._labelnames == ("tool",)


def test_record_cycle_increments_counter_and_observes_duration() -> None:
    before = metrics.REGISTRY.get_sample_value(
        "ads_orchestration_cycles_total", {"cycle": "test-cycle", "outcome": "ok"}
    )

    metrics.record_cycle("test-cycle", outcome="ok", duration_seconds=1.5)

    after = metrics.REGISTRY.get_sample_value(
        "ads_orchestration_cycles_total", {"cycle": "test-cycle", "outcome": "ok"}
    )
    assert after == (before or 0) + 1
    sum_after = metrics.REGISTRY.get_sample_value(
        "ads_orchestration_cycle_duration_seconds_sum", {"cycle": "test-cycle"}
    )
    assert sum_after is not None and sum_after >= 1.5


def test_record_step_increments_by_outcome() -> None:
    before = metrics.REGISTRY.get_sample_value(
        "ads_orchestration_steps_total", {"step": "test-step", "outcome": "failed"}
    )

    metrics.record_step("test-step", outcome="failed")

    after = metrics.REGISTRY.get_sample_value(
        "ads_orchestration_steps_total", {"step": "test-step", "outcome": "failed"}
    )
    assert after == (before or 0) + 1


def test_record_execution_outcome_increments_by_outcome() -> None:
    before = metrics.REGISTRY.get_sample_value(
        "ads_execution_outcomes_total", {"outcome": "blocked_guardrail"}
    )

    metrics.record_execution_outcome("blocked_guardrail")

    after = metrics.REGISTRY.get_sample_value(
        "ads_execution_outcomes_total", {"outcome": "blocked_guardrail"}
    )
    assert after == (before or 0) + 1


def test_record_proposal_saved_increments_by_state() -> None:
    before = metrics.REGISTRY.get_sample_value(
        "ads_proposals_saved_total", {"state": "pending"}
    )

    metrics.record_proposal_saved("pending")

    after = metrics.REGISTRY.get_sample_value(
        "ads_proposals_saved_total", {"state": "pending"}
    )
    assert after == (before or 0) + 1


def test_record_write_denial_increments_by_control() -> None:
    before = metrics.REGISTRY.get_sample_value(
        "ads_broker_write_denials_total", {"control": "daily_cap_exceeded"}
    )

    metrics.record_write_denial("daily_cap_exceeded")

    after = metrics.REGISTRY.get_sample_value(
        "ads_broker_write_denials_total", {"control": "daily_cap_exceeded"}
    )
    assert after == (before or 0) + 1


def test_record_mcp_tool_call_increments_and_observes_latency() -> None:
    before = metrics.REGISTRY.get_sample_value(
        "ads_mcp_tool_calls_total", {"tool": "test_tool", "outcome": "ok"}
    )

    metrics.record_mcp_tool_call("test_tool", outcome="ok", duration_seconds=0.2)

    after = metrics.REGISTRY.get_sample_value(
        "ads_mcp_tool_calls_total", {"tool": "test_tool", "outcome": "ok"}
    )
    assert after == (before or 0) + 1


def test_set_credential_health_fills_the_whole_bounded_grid_with_zeros() -> None:
    metrics.set_credential_health(
        {("google", "connected"): 3},
        known_platforms=("google", "meta"),
        known_statuses=("connected", "expired", "revoked", "invalid"),
    )

    assert (
        metrics.REGISTRY.get_sample_value(
            "ads_credential_health", {"platform": "google", "status": "connected"}
        )
        == 3
    )
    assert (
        metrics.REGISTRY.get_sample_value(
            "ads_credential_health", {"platform": "meta", "status": "revoked"}
        )
        == 0
    )
