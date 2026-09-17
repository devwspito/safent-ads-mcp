"""An agent may propose an explicit plan, never infer or silently repair it."""

from copy import deepcopy

import pytest

from safent_ads.mcp.presentation.opportunity_tools import ProposeCampaignArgs
from safent_ads.opportunities.infrastructure.sql_repositories import (
    _brief_payload,
    _payload_to_brief,
)
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError
from safent_ads.shared.ids import PlatformCode
from tests.integration.mcp.test_opportunity_tools import _campaign_args
from tests.unit.execution.test_campaign_creation_budget import creation_payload
from tests.unit.opportunities.test_campaign_brief import _brief


@pytest.mark.parametrize("platform", ["google", "meta"])
def test_explicit_plan_preserved_by_brief_and_sql_codec(platform):
    plan = creation_payload(platform)["creation_plan"]
    plan["name"] = "  Nombre explícito  "
    plan["daily_budget"]["amount"] = "020.00"
    original = deepcopy(plan)
    brief = _brief(platform=PlatformCode(platform), creation_plan=plan)
    plan["native"].clear()
    assert brief.creation_plan == original
    payload = _brief_payload(brief)
    assert payload["creation_plan"] == original
    assert _payload_to_brief(payload).creation_plan == original


def test_missing_plan_does_not_change_historical_brief_payload():
    assert "creation_plan" not in _brief_payload(_brief())


def test_nested_mutation_is_revalidated_before_payload_and_hash():
    brief = _brief(creation_plan=creation_payload()["creation_plan"])
    brief.creation_plan["status"] = "ACTIVE"
    with pytest.raises(CampaignCreationError, match="requires_paused"):
        _brief_payload(brief)


@pytest.mark.parametrize(
    "field,value", [("status", "ACTIVE"), ("platform", "meta"), ("native", {})]
)
def test_invalid_plan_cannot_enter_domain(field, value):
    plan = creation_payload()["creation_plan"]
    plan[field] = value
    with pytest.raises(CampaignCreationError):
        _brief(creation_plan=plan)


def test_plan_budget_must_match_brief_exactly():
    plan = creation_payload()["creation_plan"]
    plan["daily_budget"]["amount"] = "20.01"
    with pytest.raises(CampaignCreationError, match="budget_mismatch"):
        _brief(creation_plan=plan)


def test_mcp_schema_exposes_native_fields_without_inventing_defaults():
    plan = creation_payload()["creation_plan"]
    args = _campaign_args("11111111-1111-1111-1111-111111111111", "offering")
    args["creation_plan"] = plan
    parsed = ProposeCampaignArgs.model_validate(args)
    assert parsed.creation_plan.model_dump(mode="json") == plan
    schema = ProposeCampaignArgs.model_json_schema()
    assert "network_settings" in str(schema)
    assert "special_ad_categories" in str(schema)
    assert "contains_eu_political_advertising" in str(schema)
