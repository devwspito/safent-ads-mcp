"""`PlatformStateHash.compute` es determinista sobre el JSON canonico y
detecta cualquier cambio en los campos remotos (data-model.md)."""

from __future__ import annotations

import pytest

from safent_ads.accounts.domain.platform_state_hash import (
    InvalidPlatformStateHashError,
    PlatformStateHash,
)


def test_compute_is_deterministic() -> None:
    fields = {"status": "ACTIVE", "budget_minor_units": 5000}

    first = PlatformStateHash.compute(fields)
    second = PlatformStateHash.compute(fields)

    assert first == second


def test_compute_is_key_order_independent() -> None:
    in_order = PlatformStateHash.compute({"a": 1, "b": 2})
    out_of_order = PlatformStateHash.compute({"b": 2, "a": 1})

    assert in_order == out_of_order


def test_compute_changes_when_a_field_changes() -> None:
    before = PlatformStateHash.compute({"status": "ACTIVE"})
    after = PlatformStateHash.compute({"status": "PAUSED"})

    assert before != after


def test_rejects_non_hex_value() -> None:
    with pytest.raises(InvalidPlatformStateHashError):
        PlatformStateHash("not-a-hash")


def test_rejects_wrong_length() -> None:
    with pytest.raises(InvalidPlatformStateHashError):
        PlatformStateHash("abc123")
