"""`compute_diff_hash` — SHA-256 sobre JSON canonico (invariante 1)."""

from __future__ import annotations

from safent_ads.proposals.domain.diff_hash import compute_diff_hash
from safent_ads.proposals.domain.money import Money

from .conftest import entity_ref


class TestDeterminism:
    def test_same_payload_same_hash(self) -> None:
        first = compute_diff_hash(entity_ref(), "daily_budget", Money.of("100"), Money.of("70"))
        second = compute_diff_hash(entity_ref(), "daily_budget", Money.of("100"), Money.of("70"))

        assert first == second

    def test_hash_is_64_hex_chars(self) -> None:
        digest = compute_diff_hash(entity_ref(), "daily_budget", Money.of("100"), Money.of("70"))

        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


class TestSensitivityToEveryField:
    def test_different_entity_changes_hash(self) -> None:
        first = compute_diff_hash(entity_ref("1"), "daily_budget", Money.of("100"), Money.of("70"))
        second = compute_diff_hash(entity_ref("2"), "daily_budget", Money.of("100"), Money.of("70"))

        assert first != second

    def test_different_parameter_changes_hash(self) -> None:
        first = compute_diff_hash(entity_ref(), "daily_budget", Money.of("100"), Money.of("70"))
        second = compute_diff_hash(entity_ref(), "status", Money.of("100"), Money.of("70"))

        assert first != second

    def test_different_before_changes_hash(self) -> None:
        first = compute_diff_hash(entity_ref(), "daily_budget", Money.of("100"), Money.of("70"))
        second = compute_diff_hash(entity_ref(), "daily_budget", Money.of("99"), Money.of("70"))

        assert first != second

    def test_different_after_changes_hash(self) -> None:
        first = compute_diff_hash(entity_ref(), "daily_budget", Money.of("100"), Money.of("70"))
        second = compute_diff_hash(entity_ref(), "daily_budget", Money.of("100"), Money.of("71"))

        assert first != second

    def test_non_money_values_also_hash_deterministically(self) -> None:
        first = compute_diff_hash(entity_ref(), "status", "ACTIVE", "PAUSED")
        second = compute_diff_hash(entity_ref(), "status", "ACTIVE", "PAUSED")

        assert first == second
