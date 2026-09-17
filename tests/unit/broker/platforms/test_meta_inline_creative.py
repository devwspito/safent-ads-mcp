"""Inline Meta creative: real native path + Composio transport, zero provider network."""

import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from safent_ads.broker.platforms.composio_sdk_clients import ComposioMetaGraphClient
from safent_ads.broker.platforms.composio_transport import ComposioTransportError, _check_endpoint
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.platforms.meta_inline_creative import verify_inline_page
from safent_ads.broker.platforms.native_ad_child import meta_create
from safent_ads.mcp.presentation.args import ProposeAdChildArgs
from safent_ads.proposals.domain.ad_child_creation import (
    AdChildCreationError,
    validate_child_payload,
)
from safent_ads.shared.ids import PlatformCode
from tests.unit.broker.platforms.test_ad_child_creation import child_plan
from tests.unit.broker.platforms.test_composio_transport import (
    SCOPE,  # noqa: PLC0414
    metadata,
    setup_store,
    transport,
)
from tests.unit.broker.platforms.test_composio_transport import (
    scoped as scoped,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.unit.broker.platforms.test_live_meta_graph_client import _client


def inline_plan():
    plan = child_plan("meta", "ad")
    plan["native"] = {
        "name": "Approved ad",
        "creative_inline": {
            "object_story_spec": {
                "page_id": "4321",
                "link_data": {
                    "link": "https://example.com/landing",
                    "picture": "https://example.com/product.jpg",
                    "message": "Approved primary text",
                    "name": "Approved headline",
                    "description": "Approved description",
                    "call_to_action": {
                        "type": "LEARN_MORE",
                        "value": {"link": "https://example.com/landing"},
                    },
                },
            }
        },
    }
    return plan


def args(plan):
    return {
        "business_id": str(SCOPE.business_id),
        "entity_ref": f"meta:ad_set:{SCOPE.business_id}:{SCOPE.connection_id}:act_123/456",
        "child_plan": plan,
        "cause": {"text": "Explicit owner plan"},
    }


def test_inline_plan_discoverable_closed_and_exact():
    plan = inline_plan()
    assert validate_child_payload({"child_plan": plan}) == plan
    assert ProposeAdChildArgs.model_validate(args(plan)).child_plan.model_dump() == plan
    schema = ProposeAdChildArgs.model_json_schema()
    assert "creative_inline" in str(schema) and "page_id" in str(schema)


@pytest.mark.parametrize(
    "change",
    ["active", "both", "page", "secret", "http", "private", "cta", "other_link", "extra", "blank"],
)
def test_unapproved_or_incomplete_creative_rejected_at_both_boundaries(change):
    plan = inline_plan()
    story = plan["native"]["creative_inline"]["object_story_spec"]
    link = story["link_data"]
    if change == "active":
        plan["status"] = "ACTIVE"
    elif change == "both":
        plan["native"]["creative_id"] = "987"
    elif change == "page":
        story["page_id"] = "not-an-id"
    elif change == "secret":
        link["picture"] = "https://user:secret@example.com/x"
    elif change == "http":
        link["picture"] = "http://example.com/x"
    elif change == "private":
        link["picture"] = "https://127.0.0.1/x"
    elif change == "cta":
        link["call_to_action"]["type"] = "UNAPPROVED"
    elif change == "other_link":
        link["call_to_action"]["value"]["link"] = "https://other.example/x"
    elif change == "extra":
        story["instagram_actor_id"] = "555"
    else:
        link["message"] = " "
    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": plan})
    with pytest.raises(ValidationError):
        ProposeAdChildArgs.model_validate(args(plan))


def _image_hash_plan():
    """003-paquete-de-campana T112/R2.7: la forma que `packages.domain.
    platform_completeness.ad_wire_plan` proyecta para un anuncio de
    paquete -- `link_data.image_hash`, nunca `picture`."""
    plan = inline_plan()
    link_data = plan["native"]["creative_inline"]["object_story_spec"]["link_data"]
    del link_data["picture"]
    link_data["image_hash"] = "a" * 32
    return plan


def test_image_hash_link_data_validates_and_never_carries_a_url():
    plan = _image_hash_plan()

    assert validate_child_payload({"child_plan": plan}) == plan
    assert "picture" not in plan["native"]["creative_inline"]["object_story_spec"]["link_data"]


@pytest.mark.parametrize("bad_hash", ["not-hex", "DEADBEEF", "a" * 31, "a" * 65, ""])
def test_image_hash_link_data_rejects_anything_that_is_not_a_lowercase_hex_digest(bad_hash):
    plan = _image_hash_plan()
    plan["native"]["creative_inline"]["object_story_spec"]["link_data"]["image_hash"] = bad_hash

    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": plan})


def test_link_data_with_both_picture_and_image_hash_is_rejected():
    plan = _image_hash_plan()
    plan["native"]["creative_inline"]["object_story_spec"]["link_data"]["picture"] = (
        "https://example.com/x.jpg"
    )

    with pytest.raises(AdChildCreationError):
        validate_child_payload({"child_plan": plan})


@pytest.mark.parametrize(
    "failure", [None, "foreign", "missing", "masked", "cursor_loop", "over_limit"]
)
def test_page_preflight_is_get_only_same_account_bounded_and_fail_closed(failure):
    calls = []

    def call(method, path, *, params):
        calls.append((method, path, dict(params)))
        assert method == "GET" and path == ["act_123", "promote_pages"]
        if failure == "over_limit":
            body = {"data": [{"id": "111"}] * 101}
        elif failure == "cursor_loop":
            body = {
                "data": [],
                "paging": {"next": "https://untrusted.example", "cursors": {"after": "same"}},
            }
        else:
            body = {
                "data": [
                    {
                        "id": "4321"
                        if failure is None
                        else "***REDACTED***"
                        if failure == "masked"
                        else "999"
                    }
                ]
                if failure != "missing"
                else []
            }
        return SimpleNamespace(json=lambda: body)

    client = SimpleNamespace(_api_for=lambda _: SimpleNamespace(call=call))
    if failure:
        with pytest.raises(ValueError):
            verify_inline_page(client, "act_123", inline_plan()["native"]["creative_inline"])
    else:
        verify_inline_page(client, "act_123", inline_plan()["native"]["creative_inline"])
    assert len(calls) <= 2


@pytest.mark.parametrize(
    "method,endpoint",
    [
        ("POST", "/v26.0/act_123/promote_pages"),
        ("GET", "/v26.0/act_999/promote_pages"),
        ("GET", "/v26.0/4321/promote_pages"),
        ("GET", "/v26.0/act_123/promote_pages?fields=access_token"),
        ("POST", "/v26.0/act_123/adcreatives"),
    ],
)
def test_new_transport_edge_is_exact_read_only_and_never_creative_write(method, endpoint):
    with pytest.raises(ComposioTransportError):
        _check_endpoint(PlatformCode.META, "act_123", endpoint, method)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "active",
        "wrong_ad_owner",
        "wrong_creative_owner",
        "changed_page",
        "changed_image",
        "changed_text",
        "missing_story",
        "masked_id",
        "missing_id",
        "timeout",
    ],
)
def test_inline_one_post_and_exact_creative_readback(failure):
    plan = inline_plan()
    posted = []
    reads = []

    def call(method, path, *, params):
        if method == "POST":
            posted.append(deepcopy(params))
            assert path == ["act_123", "ads"] and params["status"] == "PAUSED"
            assert params["creative"] == plan["native"]["creative_inline"]
            assert not {"daily_budget", "lifetime_budget"} & params.keys()
            if failure == "timeout":
                raise TimeoutError
            return SimpleNamespace(json=lambda: {"id": "678"})
        reads.append(path)
        if path == ["678"]:
            body = {
                "name": plan["native"]["name"],
                "status": "ACTIVE" if failure == "active" else "PAUSED",
                "account_id": "999" if failure == "wrong_ad_owner" else "123",
                "adset_id": "456",
                "creative": {}
                if failure == "missing_id"
                else {"id": "***REDACTED***" if failure == "masked_id" else "987"},
            }
        else:
            assert path == ["987"]
            body = {
                "id": "987",
                "account_id": "999" if failure == "wrong_creative_owner" else "123",
                **deepcopy(plan["native"]["creative_inline"]),
            }
            if failure == "changed_page":
                body["object_story_spec"]["page_id"] = "999"
            elif failure == "changed_image":
                body["object_story_spec"]["link_data"]["picture"] = "https://other.example/x.jpg"
            elif failure == "changed_text":
                body["object_story_spec"]["link_data"]["message"] = "Changed"
            elif failure == "missing_story":
                body.pop("object_story_spec")
        return SimpleNamespace(json=lambda: body)

    if failure:
        with pytest.raises((ValueError, TimeoutError, CredentialNotConnectedError)):
            meta_create(_client(), SimpleNamespace(call=call), "act_123/456", plan)
    else:
        assert (
            meta_create(_client(), SimpleNamespace(call=call), "act_123/456", plan)["status"]
            == "PAUSED"
        )
        assert reads == [["678"], ["987"]]
    assert len(posted) == 1


def test_real_composio_client_uses_only_bound_account_and_fixed_endpoints(tmp_path):
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)
    plan = inline_plan()
    calls = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=metadata(PlatformCode.META))
        payload = json.loads(request.content)
        calls.append(payload)
        endpoint = payload["endpoint"].removeprefix("https://graph.facebook.com/v26.0/")
        data = {
            "act_123": {"currency": "EUR"},
            "456": {
                "campaign_id": "333",
                "optimization_goal": "LINK_CLICKS",
                "destination_type": "WEBSITE",
                "account_id": "123",
            },
            "333": {
                "objective": "OUTCOME_TRAFFIC",
                "buying_type": "AUCTION",
                "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                "daily_budget": "2000",
                "lifetime_budget": "0",
                "special_ad_categories": [],
                "account_id": "123",
            },
            "act_123/promote_pages": {"data": [{"id": "4321"}]},
            "act_123/ads": {"id": "678"},
            "678": {
                "name": plan["native"]["name"],
                "status": "PAUSED",
                "adset_id": "456",
                "account_id": "123",
                "creative": {"id": "987"},
            },
            "987": {"id": "987", "account_id": "123", **plan["native"]["creative_inline"]},
        }[endpoint]
        return httpx.Response(200, json={"status": 200, "data": data})

    client = ComposioMetaGraphClient(
        composio_transport=transport(reader, handler),
        credential_store=reader,
        app_id="",
        app_secret="",
    )
    client.prepare_child("act_123/456", plan)
    assert all(call["method"] == "GET" for call in calls)
    assert client.create_paused_child("act_123/456", plan)["status"] == "PAUSED"
    writes = [call for call in calls if call["method"] == "POST"]
    assert len(writes) == 1 and writes[0]["body"]["creative"] == plan["native"]["creative_inline"]
    assert all(call["connected_account_id"] == "ca_bound" for call in calls)
