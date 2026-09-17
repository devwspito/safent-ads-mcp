"""`rank_by_expected_contribution`/`split_by_attention_budget` (tasks.md
T113, NFR-11): puro, sin infraestructura."""

from __future__ import annotations

from safent_ads.opportunities.domain.campaign_brief import CampaignBrief
from safent_ads.opportunities.domain.opportunity_candidate import (
    OpportunityCandidate,
    proposal_parameter_for,
    rank_by_expected_contribution,
    split_by_attention_budget,
)
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_ID = BusinessId.new()
_ACCOUNT_REF = EntityRef(platform=PlatformCode.GOOGLE, level=EntityLevel.ACCOUNT, external_id="1")


def _candidate(*, key: str, delta_amount: str) -> OpportunityCandidate:
    brief = CampaignBrief(
        objective="Cubrir demanda",
        platform=PlatformCode.GOOGLE,
        offering_id="offering-1",
        daily_budget=Money.of("20.00", "EUR"),
        duration_days=7,
        success_criterion="CPL bajo objetivo",
        kill_criterion="Sin conversiones en 5 dias",
        angle="angulo",
        targeting_seed="semilla",
    )
    return OpportunityCandidate(
        business_id=_BUSINESS_ID,
        candidate_key=key,
        account_ref=_ACCOUNT_REF,
        brief=brief,
        expected_contribution_delta=Money.of(delta_amount, "EUR"),
        cause_sentence="causa",
    )


def test_ranks_descending_by_expected_contribution_delta() -> None:
    low = _candidate(key="low", delta_amount="10")
    high = _candidate(key="high", delta_amount="100")
    mid = _candidate(key="mid", delta_amount="50")

    ranked = rank_by_expected_contribution((low, high, mid))

    assert [c.candidate_key for c in ranked] == ["high", "mid", "low"]


def test_split_accepts_up_to_remaining_slots() -> None:
    candidates = tuple(_candidate(key=str(i), delta_amount=str(100 - i)) for i in range(5))

    split = split_by_attention_budget(candidates, remaining_slots=2)

    assert [c.candidate_key for c in split.accepted] == ["0", "1"]
    assert [c.candidate_key for c in split.deferred] == ["2", "3", "4"]


def test_split_with_no_remaining_slots_defers_everything() -> None:
    candidates = (_candidate(key="a", delta_amount="10"),)

    split = split_by_attention_budget(candidates, remaining_slots=0)

    assert split.accepted == ()
    assert split.deferred == candidates


def test_split_clamps_negative_remaining_slots_to_zero() -> None:
    candidates = (_candidate(key="a", delta_amount="10"),)

    split = split_by_attention_budget(candidates, remaining_slots=-3)

    assert split.accepted == ()
    assert split.deferred == candidates


def test_proposal_parameter_is_stable_for_the_same_candidate_key() -> None:
    key = "calendar_event:abc:google:account:1"

    assert proposal_parameter_for(key) == proposal_parameter_for(key)
    assert proposal_parameter_for(key).startswith("new_campaign:")
    assert len(proposal_parameter_for(key)) <= 64


def test_proposal_parameter_differs_for_different_candidate_keys() -> None:
    assert proposal_parameter_for("event-1") != proposal_parameter_for("event-2")
