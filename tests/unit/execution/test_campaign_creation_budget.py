"""A creation brief must never silently consume zero guardrail budget."""

from copy import deepcopy

import pytest

from safent_ads.execution.domain.guardrails import money_pair_from_diff
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError, creation_budget
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.proposals.infrastructure.value_codec import decode_value, encode_value
from safent_ads.shared.ids import EntityRef


def creation_payload(platform: str = "google") -> dict:
    native = (
        {
            "advertising_channel_type": "SEARCH",
            "bidding_strategy": "MANUAL_CPC",
            "contains_eu_political_advertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
            "network_settings": {
                "target_google_search": True,
                "target_search_network": False,
                "target_content_network": False,
                "target_partner_search_network": False,
            },
        }
        if platform == "google"
        else {
            "objective": "OUTCOME_TRAFFIC",
            "special_ad_categories": [],
            "special_ad_category_country": [],
            "buying_type": "AUCTION",
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        }
    )
    return {
        "creation_plan": {
            "schema_version": 1,
            "platform": platform,
            "name": "Explicit proposal",
            "status": "PAUSED",
            "daily_budget": {"amount": "20.00", "currency": "EUR"},
            "native": native,
        }
    }


def test_creation_guardrail_counts_actual_amount() -> None:
    diff = ProposedDiff.build(
        entity_ref=EntityRef.parse("google:account:123"),
        parameter="new_campaign:test",
        before=None,
        after=creation_payload(),
    )
    before, after = money_pair_from_diff(diff)
    assert before.amount == 0
    assert after.amount == 20


def test_prose_brief_cannot_be_signed_as_zero_budget() -> None:
    diff = ProposedDiff.build(
        entity_ref=EntityRef.parse("google:account:123"),
        parameter="new_campaign:test",
        before=None,
        after={"objective": "More leads", "daily_budget_amount": "20000"},
    )
    with pytest.raises(CampaignCreationError, match="campaign_creation_plan_required"):
        money_pair_from_diff(diff)


@pytest.mark.parametrize("platform", ["google", "meta"])
def test_creation_is_paused_and_budget_exact(platform: str) -> None:
    payload = creation_payload(platform)
    assert creation_budget(payload).amount == 20
    payload["creation_plan"]["status"] = "ACTIVE"
    with pytest.raises(CampaignCreationError, match="requires_paused"):
        creation_budget(payload)


@pytest.mark.parametrize(
    "amount", [20.0, True, "NaN", "Infinity", "0", "-1", "20.001", "2e1", "1,20", "9999999999999"]
)
def test_no_lossy_or_ambiguous_monetary_input(amount):
    payload = creation_payload()
    payload["creation_plan"]["daily_budget"]["amount"] = amount
    with pytest.raises(CampaignCreationError, match="budget_invalid"):
        creation_budget(payload)


def test_signature_binds_all_native_choices_without_roundtrip_mutation():
    ref = EntityRef.parse("google:account:123")
    payload = creation_payload()
    diff = ProposedDiff.build(
        entity_ref=ref, parameter="new_campaign:test", before=None, after=payload
    )
    roundtrip = decode_value(encode_value(payload))
    assert roundtrip == payload
    assert (
        ProposedDiff.build(
            entity_ref=ref, parameter=diff.parameter, before=None, after=roundtrip
        ).diff_hash
        == diff.diff_hash
    )
    changed = deepcopy(payload)
    changed["creation_plan"]["native"]["network_settings"]["target_search_network"] = True
    assert diff.with_new_value(changed).diff_hash != diff.diff_hash


@pytest.mark.parametrize("invalid", [[], {}, True, None])
@pytest.mark.parametrize(
    "platform,field",
    [
        ("google", "advertising_channel_type"),
        ("google", "bidding_strategy"),
        ("google", "contains_eu_political_advertising"),
        ("meta", "objective"),
        ("meta", "buying_type"),
        ("meta", "bid_strategy"),
    ],
)
def test_native_enums_are_total_for_json_and_fail_with_domain_error(platform, field, invalid):
    payload = creation_payload(platform)
    payload["creation_plan"]["native"][field] = invalid
    with pytest.raises(CampaignCreationError, match="^campaign_creation_"):
        creation_budget(payload)
