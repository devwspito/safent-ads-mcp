"""Real v25 schemas and local fake transport; no provider calls or ad creation."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from safent_ads.broker.infrastructure.in_memory_credential_store import InMemoryCredentialStore
from safent_ads.broker.platforms.composio_sdk_clients import _GoogleSdkFacade
from safent_ads.broker.platforms.errors import GaqlValidationError
from safent_ads.broker.platforms.gaql_validator import validate_gaql
from safent_ads.broker.platforms.live_google_ads_client import LiveGoogleAdsSearchClient
from safent_ads.broker.platforms.native_ad_child import google_create
from safent_ads.mcp.presentation.ad_child_args import GoogleAdGroupPlanArgs
from safent_ads.mcp.presentation.campaign_creation_args import GoogleCampaignCreationArgs
from safent_ads.proposals.domain.ad_child_creation import validate_child_payload
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError, creation_budget
from safent_ads.proposals.domain.google_search_targeting import valid_keywords
from tests.unit.broker.platforms.test_ad_child_creation import child_plan
from tests.unit.execution.test_campaign_creation_budget import creation_payload

_KEYWORDS = [{"text": "consulta veterinaria", "match_type": "EXACT"}]
_GEOGRAPHY = {
    "geo_target_constants": ["geoTargetConstants/1005424", "geoTargetConstants/1005493"],
    "positive_geo_target_type": "PRESENCE",
}


@pytest.mark.parametrize(
    "query",
    [
        "SELECT geo_target_constant.resource_name, geo_target_constant.name, "
        "geo_target_constant.status FROM geo_target_constant "
        "WHERE geo_target_constant.name = 'Madrid'",
        "SELECT campaign_criterion.resource_name, campaign_criterion.location.geo_target_constant "
        "FROM campaign_criterion WHERE campaign.id = 456",
    ],
)
def test_geography_discovery_and_verification_are_allowed_reads(query):
    validate_gaql(query)


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM campaign_criterion",
        "SELECT geo_target_constant.name FROM geo_target_constant; DELETE FROM campaign",
        "SELECT x FROM billing_setup",
    ],
)
def test_geography_allowlist_does_not_allow_writes_or_other_resources(query):
    with pytest.raises(GaqlValidationError):
        validate_gaql(query)


@pytest.mark.parametrize("match", ["EXACT", "PHRASE", "BROAD"])
def test_keyword_match_is_explicit_and_signed_schema_roundtrips(match):
    plan = child_plan()
    plan["native"]["keywords"] = [{"text": "consulta veterinaria", "match_type": match}]
    assert validate_child_payload({"child_plan": plan}) == plan
    assert GoogleAdGroupPlanArgs.model_validate(plan).model_dump(mode="json") == plan


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        [True],
        [{"text": "hello"}],
        [{"text": "", "match_type": "EXACT"}],
        [{"text": " x", "match_type": "EXACT"}],
        [{"text": "x\n", "match_type": "EXACT"}],
        [{"text": "x" * 81, "match_type": "EXACT"}],
        [{"text": "x", "match_type": "EXACT", "negative": True}],
        [{"text": "x", "match_type": "AUTO"}],
        _KEYWORDS * 2,
        _KEYWORDS * 51,
    ],
)
def test_invalid_keywords_rejected_without_normalization(bad):
    assert not valid_keywords(bad)
    plan = child_plan()
    plan["native"]["keywords"] = bad
    with pytest.raises(CampaignCreationError):
        validate_child_payload({"child_plan": plan})


def test_legacy_schemas_do_not_add_null_targeting_fields():
    child = child_plan()
    campaign = creation_payload()["creation_plan"]
    assert GoogleAdGroupPlanArgs.model_validate(child).model_dump(mode="json") == child
    assert GoogleCampaignCreationArgs.model_validate(campaign).model_dump(mode="json") == campaign


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {**_GEOGRAPHY, "positive_geo_target_type": "PRESENCE_OR_INTEREST"},
        {**_GEOGRAPHY, "geo_target_constants": []},
        {**_GEOGRAPHY, "geo_target_constants": ["geoTargetConstants/1"] * 2},
        {**_GEOGRAPHY, "geo_target_constants": ["geoTargetConstants/0"]},
        {**_GEOGRAPHY, "geo_target_constants": ["customers/123/campaigns/1"]},
        {**_GEOGRAPHY, "geo_target_constants": [123]},
        {**_GEOGRAPHY, "geo_target_constants": ["geoTargetConstants/1' OR 1=1"]},
        {**_GEOGRAPHY, "extra": True},
    ],
)
def test_geography_is_explicit_bounded_presence_only(bad):
    payload = creation_payload()
    payload["creation_plan"]["native"]["geographic_targeting"] = bad
    with pytest.raises(CampaignCreationError, match="geography_invalid"):
        creation_budget(payload)


def _sdk():
    return GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)


@pytest.mark.parametrize(
    "failure", [None, "account", "location", "interest", "negative", "partial"]
)
def test_campaign_geography_atomic_mutation_and_exact_ack(monkeypatch, failure):
    sdk, calls = _sdk(), []
    payload = creation_payload()
    payload["creation_plan"]["native"]["geographic_targeting"] = deepcopy(_GEOGRAPHY)
    plan = payload["creation_plan"]
    assert GoogleCampaignCreationArgs.model_validate(plan).model_dump(mode="json") == plan

    def mutate(**kwargs):
        calls.append(kwargs)
        assert kwargs["partial_failure"] is False and kwargs["retry"] is None
        budget_op, campaign_op, *criteria = kwargs["mutate_operations"]
        campaign = campaign_op.campaign_operation.create
        assert campaign.resource_name == "customers/123/campaigns/-2"
        assert campaign.status.name == "PAUSED"
        assert campaign.geo_target_type_setting.positive_geo_target_type.name == "PRESENCE"
        response = sdk.get_type("MutateGoogleAdsResponse")
        b, c = sdk.get_type("MutateOperationResponse"), sdk.get_type("MutateOperationResponse")
        b.campaign_budget_result.resource_name = "customers/123/campaignBudgets/456"
        b.campaign_budget_result.campaign_budget = budget_op.campaign_budget_operation.create
        c.campaign_result.resource_name = "customers/123/campaigns/789"
        c.campaign_result.campaign = campaign
        c.campaign_result.campaign.campaign_budget = b.campaign_budget_result.resource_name
        if failure == "interest":
            c.campaign_result.campaign.geo_target_type_setting.positive_geo_target_type = (
                "PRESENCE_OR_INTEREST"
            )
        response.mutate_operation_responses.extend([b, c])
        for index, operation in enumerate(criteria):
            criterion = operation.campaign_criterion_operation.create
            assert criterion.campaign == "customers/123/campaigns/-2"
            assert (
                criterion.location.geo_target_constant == _GEOGRAPHY["geo_target_constants"][index]
            )
            r = sdk.get_type("MutateOperationResponse")
            r.campaign_criterion_result.resource_name = (
                f"customers/123/campaignCriteria/789~{index + 1}"
            )
            actual = r.campaign_criterion_result.campaign_criterion
            sdk.copy_from(actual, criterion)
            actual.campaign = "customers/123/campaigns/789"
            if failure == "account":
                actual.campaign = "customers/999/campaigns/789"
            if failure == "location":
                actual.location.geo_target_constant = "geoTargetConstants/999"
            if failure == "negative":
                actual.negative = True
            response.mutate_operation_responses.append(r)
        if failure == "partial":
            response.partial_failure_error.code = 13
        return response

    monkeypatch.setattr(sdk, "get_service", lambda _: SimpleNamespace(mutate=mutate))
    client = LiveGoogleAdsSearchClient(
        client_id="test",
        client_secret="test",
        credential_store=InMemoryCredentialStore(),
    )
    monkeypatch.setattr(client, "_resolve_credential", lambda _: None)
    monkeypatch.setattr(client, "_build_sdk_client", lambda _: sdk)
    if failure:
        with pytest.raises(ValueError):
            client.create_paused_campaign("123", payload)
    else:
        assert client.create_paused_campaign("123", payload)["status"] == "PAUSED"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure", [None, "text", "match", "active", "negative", "account", "partial"]
)
def test_keywords_share_the_paused_group_mutation_and_exact_ack(monkeypatch, failure):
    sdk, calls = _sdk(), []
    plan = child_plan()
    plan["native"]["keywords"] = deepcopy(_KEYWORDS)

    def mutate(**kwargs):
        calls.append(kwargs)
        assert kwargs["partial_failure"] is False and kwargs["retry"] is None
        group_op, keyword_op = kwargs["mutate_operations"]
        group = group_op.ad_group_operation.create
        keyword = keyword_op.ad_group_criterion_operation.create
        assert group.resource_name == "customers/123/adGroups/-1"
        assert keyword.ad_group == group.resource_name
        assert group.status.name == keyword.status.name == "PAUSED"
        assert keyword.keyword.match_type.name == "EXACT"
        response = sdk.get_type("MutateGoogleAdsResponse")
        g, k = sdk.get_type("MutateOperationResponse"), sdk.get_type("MutateOperationResponse")
        g.ad_group_result.resource_name = "customers/123/adGroups/99"
        g.ad_group_result.ad_group = group
        k.ad_group_criterion_result.resource_name = "customers/123/adGroupCriteria/99~77"
        actual = k.ad_group_criterion_result.ad_group_criterion
        sdk.copy_from(actual, keyword)
        actual.ad_group = "customers/123/adGroups/99"
        if failure == "text":
            actual.keyword.text = "different"
        if failure == "match":
            actual.keyword.match_type = "BROAD"
        if failure == "active":
            actual.status = "ENABLED"
        if failure == "negative":
            actual.negative = True
        if failure == "account":
            actual.ad_group = "customers/999/adGroups/99"
        response.mutate_operation_responses.extend([g, k])
        if failure == "partial":
            response.partial_failure_error.code = 13
        return response

    monkeypatch.setattr(sdk, "get_service", lambda _: SimpleNamespace(mutate=mutate))
    if failure:
        with pytest.raises(ValueError):
            google_create(sdk, "customers/123/campaigns/456", plan)
    else:
        assert google_create(sdk, "customers/123/campaigns/456", plan)["status"] == "PAUSED"
    assert len(calls) == 1


def test_composio_facade_preserves_approved_keywords_without_retry():
    calls = []

    class Transport:
        def request(self, platform, account, **kwargs):
            calls.append((platform, account, kwargs))
            raise TimeoutError("synthetic uncertain response")

    plan = child_plan()
    plan["native"]["keywords"] = deepcopy(_KEYWORDS)
    facade = _GoogleSdkFacade(Transport(), "123")
    with pytest.raises(TimeoutError):
        google_create(facade, "customers/123/campaigns/456", plan)
    assert len(calls) == 1
    _, account, request = calls[0]
    assert account == "123"
    assert request["endpoint"] == "/v25/customers/123/googleAds:mutate"
    assert request["method"] == "POST"
    group, keyword = request["body"]["mutateOperations"]
    assert group["adGroupOperation"]["create"]["status"] == "PAUSED"
    created = keyword["adGroupCriterionOperation"]["create"]
    assert created["status"] == "PAUSED"
    assert created["adGroup"] == "customers/123/adGroups/-1"
    assert created["keyword"] == {"text": "consulta veterinaria", "matchType": "EXACT"}
