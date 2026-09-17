"""`GuardrailPolicy.clamp()`: tope diario/mensual, suelo/techo, salto maximo
y cambios maximos por dia (data-model.md §GuardrailSet; spec.md FR-13)."""

from __future__ import annotations

import pytest

from safent_ads.rules.domain.errors import InvalidGuardrailPolicyError
from safent_ads.rules.domain.guardrail import GuardrailPolicy


def _policy(**overrides: object) -> GuardrailPolicy:
    defaults: dict[str, object] = {
        "daily_cap_minor": 100_000,
        "monthly_cap_minor": 2_000_000,
        "floor_minor": 1_000,
        "ceiling_minor": 50_000,
        "max_step_pct": 20.0,
        "max_changes_per_day": 3,
    }
    defaults.update(overrides)
    return GuardrailPolicy(**defaults)  # type: ignore[arg-type]


def test_rejects_floor_above_ceiling() -> None:
    with pytest.raises(InvalidGuardrailPolicyError):
        _policy(floor_minor=60_000, ceiling_minor=50_000)


@pytest.mark.parametrize("max_step_pct", [0, -5, 101])
def test_rejects_max_step_pct_out_of_bounds(max_step_pct: float) -> None:
    with pytest.raises(InvalidGuardrailPolicyError):
        _policy(max_step_pct=max_step_pct)


def test_rejects_negative_caps() -> None:
    with pytest.raises(InvalidGuardrailPolicyError):
        _policy(daily_cap_minor=-1)


def test_passes_through_when_within_every_limit() -> None:
    policy = _policy()

    verdict = policy.clamp(
        current_minor=10_000, proposed_minor=11_000, changes_today=0, spent_month_minor=0
    )

    assert verdict.allowed_value_minor == 11_000
    assert verdict.clamped is False
    assert verdict.blocked is False
    assert verdict.breached_limits == ()


def test_clamps_to_max_step_pct() -> None:
    policy = _policy(max_step_pct=20.0)

    verdict = policy.clamp(
        current_minor=10_000, proposed_minor=15_000, changes_today=0, spent_month_minor=0
    )

    assert verdict.allowed_value_minor == 12_000  # 10_000 * 1.20
    assert verdict.clamped is True
    assert "max_step_pct" in verdict.breached_limits


def test_clamps_a_decrease_to_max_step_pct_too() -> None:
    policy = _policy(max_step_pct=20.0)

    verdict = policy.clamp(
        current_minor=10_000, proposed_minor=5_000, changes_today=0, spent_month_minor=0
    )

    assert verdict.allowed_value_minor == 8_000  # 10_000 * 0.80
    assert "max_step_pct" in verdict.breached_limits


def test_clamps_to_floor() -> None:
    policy = _policy(floor_minor=9_000, max_step_pct=90.0)

    verdict = policy.clamp(
        current_minor=10_000, proposed_minor=1_000, changes_today=0, spent_month_minor=0
    )

    assert verdict.allowed_value_minor == 9_000
    assert "floor" in verdict.breached_limits


def test_clamps_to_ceiling() -> None:
    policy = _policy(ceiling_minor=10_500, max_step_pct=90.0)

    verdict = policy.clamp(
        current_minor=10_000, proposed_minor=19_000, changes_today=0, spent_month_minor=0
    )

    assert verdict.allowed_value_minor == 10_500
    assert "ceiling" in verdict.breached_limits


def test_clamps_to_daily_cap() -> None:
    policy = _policy(daily_cap_minor=10_200, ceiling_minor=50_000, max_step_pct=90.0)

    verdict = policy.clamp(
        current_minor=10_000, proposed_minor=15_000, changes_today=0, spent_month_minor=0
    )

    assert verdict.allowed_value_minor == 10_200
    assert "daily_cap" in verdict.breached_limits


def test_clamps_to_remaining_monthly_cap() -> None:
    policy = _policy(monthly_cap_minor=100_000, max_step_pct=90.0, ceiling_minor=50_000)

    verdict = policy.clamp(
        current_minor=10_000, proposed_minor=18_000, changes_today=0, spent_month_minor=95_000
    )

    assert verdict.allowed_value_minor == 15_000  # 10_000 + (100_000-95_000) restante
    assert "monthly_cap" in verdict.breached_limits


def test_blocks_when_max_changes_per_day_reached() -> None:
    policy = _policy(max_changes_per_day=2)

    verdict = policy.clamp(
        current_minor=10_000, proposed_minor=11_000, changes_today=2, spent_month_minor=0
    )

    assert verdict.blocked is True
    assert verdict.allowed_value_minor == 10_000
    assert verdict.breached_limits == ("max_changes_per_day",)


def test_step_clamp_skipped_when_current_is_zero() -> None:
    policy = _policy(ceiling_minor=5_000)

    verdict = policy.clamp(
        current_minor=0, proposed_minor=3_000, changes_today=0, spent_month_minor=0
    )

    assert verdict.allowed_value_minor == 3_000
    assert "max_step_pct" not in verdict.breached_limits
