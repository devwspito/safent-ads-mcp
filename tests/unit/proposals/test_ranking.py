"""`rank_proposals` (profitability-engine.md §8: "la cola cambia de eje" —
`expected_contribution_delta DESC`, `urgency` como desempate, sin
contribucion al final)."""

from __future__ import annotations

from datetime import timedelta

from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Urgency
from safent_ads.proposals.domain.ranking import rank_proposals

from .conftest import NOW, make_proposal


class TestOrdersByContributionDeltaDescending:
    def test_higher_contribution_comes_first(self) -> None:
        low = make_proposal(expected_contribution_delta=Money.of("10"))
        high = make_proposal(expected_contribution_delta=Money.of("75"))

        ranked = rank_proposals([low, high])

        assert ranked == (high, low)

    def test_urgency_breaks_ties_on_equal_contribution(self) -> None:
        minor = make_proposal(urgency=Urgency.MINOR, expected_contribution_delta=Money.of("50"))
        critical = make_proposal(
            urgency=Urgency.CRITICAL, expected_contribution_delta=Money.of("50")
        )

        ranked = rank_proposals([minor, critical])

        assert ranked == (critical, minor)


class TestProposalsWithoutContributionGoLast:
    def test_missing_delta_sorts_after_any_positive_delta(self) -> None:
        with_delta = make_proposal(urgency=Urgency.MINOR, expected_contribution_delta=Money.of("1"))
        without_delta = make_proposal(urgency=Urgency.CRITICAL, expected_contribution_delta=None)

        ranked = rank_proposals([without_delta, with_delta])

        assert ranked == (with_delta, without_delta)

    def test_missing_delta_group_still_orders_by_urgency(self) -> None:
        minor = make_proposal(urgency=Urgency.MINOR, expected_contribution_delta=None)
        recommended = make_proposal(urgency=Urgency.RECOMMENDED, expected_contribution_delta=None)
        critical = make_proposal(urgency=Urgency.CRITICAL, expected_contribution_delta=None)

        ranked = rank_proposals([minor, critical, recommended])

        assert ranked == (critical, recommended, minor)


class TestExpiryIsTheFinalTiebreak:
    def test_earlier_expiry_wins_when_everything_else_is_equal(self) -> None:
        soon = make_proposal(ttl_hours=1, expected_contribution_delta=Money.of("20"))
        later = make_proposal(ttl_hours=48, expected_contribution_delta=Money.of("20"))

        ranked = rank_proposals([later, soon])

        assert ranked == (soon, later)
        assert soon.expires_at < later.expires_at
        assert soon.expires_at == NOW + timedelta(hours=1)
