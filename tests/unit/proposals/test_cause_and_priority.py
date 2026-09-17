"""`Cause`, `CauseKey`, `Urgency`, `Priority`, `ExpiryPolicy`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.proposals.domain.cause import Cause, CauseInvariantError
from safent_ads.proposals.domain.priority import ExpiryPolicy, Urgency

NOW = datetime(2026, 9, 9, 10, 0, tzinfo=UTC)


class TestCauseTextLimit:
    def test_empty_text_rejected(self) -> None:
        with pytest.raises(CauseInvariantError):
            Cause(text="")

    def test_over_140_chars_rejected(self) -> None:
        with pytest.raises(CauseInvariantError):
            Cause(text="x" * 141)

    def test_exactly_140_chars_accepted(self) -> None:
        Cause(text="x" * 140)


class TestExpiryPolicy:
    def test_critical_urgency_gets_24h(self) -> None:
        policy = ExpiryPolicy()

        result = policy.expires_at(Urgency.CRITICAL, NOW)

        assert (result - NOW).total_seconds() == 24 * 3600

    def test_recommended_urgency_gets_72h(self) -> None:
        policy = ExpiryPolicy()

        result = policy.expires_at(Urgency.RECOMMENDED, NOW)

        assert (result - NOW).total_seconds() == 72 * 3600

    def test_minor_urgency_gets_72h(self) -> None:
        policy = ExpiryPolicy()

        result = policy.expires_at(Urgency.MINOR, NOW)

        assert (result - NOW).total_seconds() == 72 * 3600
