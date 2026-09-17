"""Real installed protobuf contracts, fake SDK transport; never provider network."""

from types import SimpleNamespace

import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.auth.credentials import AnonymousCredentials

from safent_ads.broker.infrastructure.in_memory_credential_store import InMemoryCredentialStore
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.platforms.live_google_ads_client import LiveGoogleAdsSearchClient
from tests.unit.broker.platforms.test_live_meta_graph_client import _client
from tests.unit.execution.test_campaign_creation_budget import creation_payload


@pytest.mark.parametrize("partial", [False, True])
def test_google_budget_and_campaign_atomic_paused_native_contract(monkeypatch, partial):
    sdk = GoogleAdsClient(credentials=AnonymousCredentials(), version="v25", use_proto_plus=True)
    calls = []

    def mutate(**kwargs):
        calls.append(kwargs)
        assert kwargs["partial_failure"] is False
        assert kwargs["retry"] is None
        budget_op, campaign_op = kwargs["mutate_operations"]
        budget = budget_op.campaign_budget_operation.create
        campaign = campaign_op.campaign_operation.create
        assert budget.amount_micros == 20_000_000
        assert campaign.campaign_budget == "customers/123/campaignBudgets/-1"
        assert campaign.status.name == "PAUSED"
        assert campaign.advertising_channel_type.name == "SEARCH"
        assert campaign._pb.HasField("manual_cpc")
        assert (
            campaign.contains_eu_political_advertising.name
            == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
        )
        assert campaign.network_settings.target_google_search is True
        assert campaign.network_settings.target_content_network is False
        response = sdk.get_type("MutateGoogleAdsResponse")
        if partial:
            response.partial_failure_error.code = 13
            return response
        b = sdk.get_type("MutateOperationResponse")
        b.campaign_budget_result.resource_name = "customers/123/campaignBudgets/456"
        b.campaign_budget_result.campaign_budget = budget
        c = sdk.get_type("MutateOperationResponse")
        c.campaign_result.resource_name = "customers/123/campaigns/789"
        c.campaign_result.campaign = campaign
        c.campaign_result.campaign.campaign_budget = b.campaign_budget_result.resource_name
        response.mutate_operation_responses.extend([b, c])
        return response

    monkeypatch.setattr(sdk, "get_service", lambda _: SimpleNamespace(mutate=mutate))
    client = LiveGoogleAdsSearchClient(
        client_id="test", client_secret="test", credential_store=InMemoryCredentialStore()
    )
    monkeypatch.setattr(client, "_resolve_credential", lambda _: None)
    monkeypatch.setattr(client, "_build_sdk_client", lambda _: sdk)
    if partial:
        with pytest.raises(ValueError, match="partial_response"):
            client.create_paused_campaign("123", creation_payload())
    else:
        result = client.create_paused_campaign("123", creation_payload())
        assert result["campaign_resource"] == "customers/123/campaigns/789"
        assert result["budget_resource"] == "customers/123/campaignBudgets/456"
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [None, "timeout", "wrong_account", "active", "wrong_budget"])
def test_meta_native_minor_units_no_retry_and_confirmation(monkeypatch, failure):
    client = _client()
    calls = []

    def call(method, path, *, params):
        calls.append((method, path, params))
        if method == "POST":
            assert path == ["act_123", "campaigns"]
            assert params == {
                "name": "Explicit proposal",
                "status": "PAUSED",
                "daily_budget": 2000,
                "objective": "OUTCOME_TRAFFIC",
                "special_ad_categories": [],
                "special_ad_category_country": [],
                "buying_type": "AUCTION",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            }
            if failure == "timeout":
                raise TimeoutError("unknown remote outcome")
            return SimpleNamespace(json=lambda: {"id": "456"})
        result = {**calls[0][2], "account_id": "123"}
        if failure == "wrong_account":
            result["account_id"] = "999"
        if failure == "active":
            result["status"] = "ACTIVE"
        if failure == "wrong_budget":
            result["daily_budget"] = "99999"
        return SimpleNamespace(json=lambda: result)

    monkeypatch.setattr(client, "_api_for", lambda _: SimpleNamespace(call=call))
    if failure:
        with pytest.raises((TimeoutError, ValueError, CredentialNotConnectedError)):
            client.create_paused_campaign("act_123", creation_payload("meta"))
    else:
        result = client.create_paused_campaign("act_123", creation_payload("meta"))
        assert result["campaign_resource"] == "act_123/456"
    assert len([call for call in calls if call[0] == "POST"]) == 1


@pytest.mark.parametrize("optional_country", [None, [], "omitted"])
def test_meta_optional_country_only_empty_when_no_special_category(monkeypatch, optional_country):
    client = _client()
    payload = creation_payload("meta")
    posted = {}

    def call(method, path, *, params):
        if method == "POST":
            posted.update(params)
            return SimpleNamespace(json=lambda: {"id": "456"})
        result = {**posted, "account_id": "123", "special_ad_categories": ["NONE"]}
        if optional_country == "omitted":
            result.pop("special_ad_category_country")
        else:
            result["special_ad_category_country"] = optional_country
        return SimpleNamespace(json=lambda: result)

    monkeypatch.setattr(client, "_api_for", lambda _: SimpleNamespace(call=call))
    assert client.create_paused_campaign("act_123", payload)["campaign_resource"] == "act_123/456"


@pytest.mark.parametrize("countries", [["US", "ES"], ["ES"], ["ES", "US", "GB"], None])
def test_meta_special_scopes_compared_as_exact_sets(monkeypatch, countries):
    client = _client()
    payload = creation_payload("meta")
    payload["creation_plan"]["native"].update(
        special_ad_categories=["HOUSING", "EMPLOYMENT"], special_ad_category_country=["ES", "US"]
    )
    posted = {}

    def call(method, path, *, params):
        if method == "POST":
            posted.update(params)
            return SimpleNamespace(json=lambda: {"id": "456"})
        return SimpleNamespace(
            json=lambda: {
                **posted,
                "account_id": "123",
                "special_ad_categories": ["EMPLOYMENT", "HOUSING"],
                "special_ad_category_country": countries,
            }
        )

    monkeypatch.setattr(client, "_api_for", lambda _: SimpleNamespace(call=call))
    if countries == ["US", "ES"]:
        assert client.create_paused_campaign("act_123", payload)["status"] == "PAUSED"
    else:
        with pytest.raises(ValueError, match="confirmation_mismatch"):
            client.create_paused_campaign("act_123", payload)
