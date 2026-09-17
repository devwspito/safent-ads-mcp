"""`config/caps.yaml`: fail-closed en fichero ausente/invalido, `defaults`
rellena los campos de comportamiento ausentes por cuenta, monetarios siempre
obligatorios (T066, plan.md §15 D-A1, threat-model.md C-17)."""

from __future__ import annotations

from pathlib import Path

import pytest

from safent_ads.broker.infrastructure.caps_config import (
    AccountCaps,
    CapsConfigError,
    load_caps_config,
    parse_caps_config,
)

_VALID_YAML = """
defaults:
  max_step_pct: 30
  max_changes_per_day: 2
  autonomy_enabled: false

accounts:
  "acc-1":
    daily_cap_minor: 10000
    monthly_cap_minor: 300000
    floor_minor: 500
    ceiling_minor: 50000
  "acc-2":
    daily_cap_minor: 20000
    monthly_cap_minor: 600000
    floor_minor: 1000
    ceiling_minor: 90000
    max_step_pct: 10
    max_changes_per_day: 1
    autonomy_enabled: true
"""


def test_resolve_applies_defaults_when_account_omits_behaviour_fields() -> None:
    config = parse_caps_config(_VALID_YAML)

    resolved = config.resolve("acc-1")

    assert resolved == AccountCaps(
        daily_cap_minor=10000,
        monthly_cap_minor=300000,
        floor_minor=500,
        ceiling_minor=50000,
        max_step_pct=30,
        max_changes_per_day=2,
        autonomy_enabled=False,
    )


def test_resolve_keeps_explicit_account_overrides() -> None:
    config = parse_caps_config(_VALID_YAML)

    resolved = config.resolve("acc-2")

    assert resolved.max_step_pct == 10
    assert resolved.max_changes_per_day == 1
    assert resolved.autonomy_enabled is True


def test_resolve_raises_for_unknown_account() -> None:
    config = parse_caps_config(_VALID_YAML)

    with pytest.raises(CapsConfigError, match="acc-unknown"):
        config.resolve("acc-unknown")


def test_rejects_floor_above_ceiling() -> None:
    raw = _VALID_YAML.replace("floor_minor: 500", "floor_minor: 999999")

    with pytest.raises(CapsConfigError):
        parse_caps_config(raw)


@pytest.mark.parametrize("max_step_pct", [0, -5, 101])
def test_rejects_max_step_pct_out_of_bounds(max_step_pct: float) -> None:
    raw = f"""
defaults:
  max_step_pct: {max_step_pct}
  max_changes_per_day: 2
  autonomy_enabled: false
accounts: {{}}
"""

    with pytest.raises(CapsConfigError):
        parse_caps_config(raw)


def test_rejects_negative_caps() -> None:
    raw = _VALID_YAML.replace("daily_cap_minor: 10000", "daily_cap_minor: -1")

    with pytest.raises(CapsConfigError):
        parse_caps_config(raw)


def test_rejects_unknown_top_level_field() -> None:
    raw = _VALID_YAML + "\nsomething_unexpected: true\n"

    with pytest.raises(CapsConfigError):
        parse_caps_config(raw)


def test_rejects_invalid_yaml() -> None:
    with pytest.raises(CapsConfigError, match="YAML invalido"):
        parse_caps_config("defaults: [unclosed")


def test_rejects_non_mapping_root() -> None:
    with pytest.raises(CapsConfigError, match="mapeo YAML"):
        parse_caps_config("- just\n- a\n- list\n")


def test_load_raises_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "caps.yaml"

    with pytest.raises(CapsConfigError, match="no encontrado"):
        load_caps_config(missing)


def test_load_reads_and_parses_a_real_file(tmp_path: Path) -> None:
    caps_path = tmp_path / "caps.yaml"
    caps_path.write_text(_VALID_YAML, encoding="utf-8")

    config = load_caps_config(caps_path)

    assert config.resolve("acc-1").daily_cap_minor == 10000


def test_empty_accounts_block_resolves_nothing() -> None:
    raw = """
defaults:
  max_step_pct: 30
  max_changes_per_day: 2
  autonomy_enabled: false
"""

    config = parse_caps_config(raw)

    with pytest.raises(CapsConfigError):
        config.resolve("any")
