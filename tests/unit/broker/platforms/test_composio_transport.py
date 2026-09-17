"""No external requests: encrypted real bindings, official SDK schemas, HTTP doubles."""

import asyncio
import base64
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from safent_ads.accounts.domain.refs import CredentialRefId
from safent_ads.broker.application.connection_scope import ConnectionScope, connection_scope
from safent_ads.broker.application.ports import ComposioAccountBinding, CredentialRecord
from safent_ads.broker.infrastructure.connected_credential_store import ConnectedCredentialStore
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.broker.platforms.composio_sdk_clients import (
    ComposioGoogleAdsSearchClient,
    ComposioMetaAdLibraryClient,
    ComposioMetaGraphClient,
)
from safent_ads.broker.platforms.composio_transport import (
    ComposioAdsTransport,
    ComposioTransportError,
    _check_endpoint,
)
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.platforms.meta_ad_library import MetaAdLibraryIdentityRequiredError
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import EntityLevel, PlatformCode
from tests.unit.execution.test_campaign_creation_budget import creation_payload

NOW = datetime(2026, 9, 13, tzinfo=UTC)
BINDING = ComposioAccountBinding("ca_bound", "safent:user-1", "ac_bound", "999")
SCOPE = ConnectionScope(uuid4(), uuid4())
SCOPE_ARGS = {"business_id": str(SCOPE.business_id), "connection_id": str(SCOPE.connection_id)}


@pytest.fixture(autouse=True)
def scoped():
    with connection_scope(SCOPE):
        yield


def setup_store(path, platform=PlatformCode.GOOGLE):
    encrypted = EncryptedCredentialStore(path, base64.b64encode(b"m" * 32).decode())
    ref = CredentialRefId(uuid4())
    record = CredentialRecord(
        platform,
        json.dumps(asdict(BINDING)),
        "composio_connection",
        ("ads_management",),
        NOW,
        None,
        **SCOPE_ARGS,
    )
    encrypted.save_credential(ref, record)
    account = "123" if platform == PlatformCode.GOOGLE else "act_123"
    encrypted.bind_account_credential(platform, account, ref, **SCOPE_ARGS)
    reader = ConnectedCredentialStore(encrypted, FixedClock(NOW))
    return encrypted, reader, ref, record


def metadata(platform=PlatformCode.GOOGLE):
    return {
        "id": BINDING.connected_account_id,
        "user_id": BINDING.user_id,
        "status": "ACTIVE",
        "toolkit": {"slug": "googleads" if platform == PlatformCode.GOOGLE else "metaads"},
        "auth_config": {"id": BINDING.auth_config_id},
    }


def transport(reader, handler):
    return ComposioAdsTransport(
        api_key="private-broker-key",
        credential_store=reader,
        http_transport=httpx.MockTransport(handler),
    )


def request(transport, **overrides):
    args = {
        "platform": PlatformCode.GOOGLE,
        "native_account_id": "123",
        "endpoint": "/v25/customers/123/googleAds:searchStream",
        "method": "POST",
        "body": {"query": "SELECT customer.currency_code FROM customer"},
    }
    return transport.request(**{**args, **overrides})


def test_masked_customer_resource_names_are_restored_only_in_typed_fields(tmp_path):
    _, reader, _, _ = setup_store(tmp_path)
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata())
        return httpx.Response(200, json={"status": 200, "data": [{"results": [{
            "campaign": {
                "resourceName": "customers/***REDACTED***/campaigns/456",
                "campaignBudget": "customers/***REDACTED***/campaignBudgets/789",
                "name": "customers/***REDACTED***/campaigns/456",
                "id": "456",
            },
            "customer": {"resourceName": "customers/***REDACTED***", "id": "***REDACTED***"},
        }]}]})
    data = request(transport(reader, handler))[0]["results"][0]
    # This fixture is an MCC binding: Composio masks the manager (999),
    # never replace that with the child account (123) being queried.
    assert data["campaign"]["resourceName"] == "customers/999/campaigns/456"
    assert data["campaign"]["campaignBudget"] == "customers/999/campaignBudgets/789"
    assert data["campaign"]["name"] == "customers/***REDACTED***/campaigns/456"
    assert data["campaign"]["id"] == "456"
    assert data["customer"]["id"] == "999"


def test_direct_customer_redaction_restores_selected_account_not_other_ids(tmp_path):
    encrypted, reader, ref, record = setup_store(tmp_path)
    direct_binding = replace(BINDING, login_customer_id=None)
    encrypted.save_credential(ref, replace(record, token=json.dumps(asdict(direct_binding))))
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata())
        return httpx.Response(200, json={"status": 200, "data": {
            "results": [{"resourceName": "customers/***REDACTED***/campaigns/456"}],
        }})
    assert request(transport(reader, handler))["results"] == [
        {"resourceName": "customers/123/campaigns/456"},
    ]


def test_customer_normalization_never_rewrites_different_account_or_entity_id(tmp_path):
    _, reader, _, _ = setup_store(tmp_path)
    original = {"resourceName": "customers/666/campaigns/456", "id": "***REDACTED***"}
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata())
        return httpx.Response(200, json={"status": 200, "data": original})
    assert request(transport(reader, handler)) == original


def test_binding_is_encrypted_and_never_becomes_native_token(tmp_path):
    encrypted, reader, ref, record = setup_store(tmp_path)
    credential = asyncio.run(reader.get_credential(PlatformCode.GOOGLE, "123"))
    assert credential.composio == BINDING
    assert credential.refresh_token is None and credential.access_token is None
    assert "ca_bound" not in repr(credential)
    assert all(b"ca_bound" not in path.read_bytes() for path in tmp_path.rglob("*.enc"))
    assert asyncio.run(reader.get_credential(PlatformCode.GOOGLE, "456")) is None
    with connection_scope(ConnectionScope(uuid4(), uuid4())):
        assert asyncio.run(reader.get_credential(PlatformCode.GOOGLE, "123")) is None
    with connection_scope(None):
        assert asyncio.run(reader.get_credential(PlatformCode.GOOGLE, "123")) is None
    for denied in (replace(record, revoked_at=NOW), replace(record, expires_at=NOW)):
        encrypted.save_credential(ref, denied)
        assert asyncio.run(reader.get_credential(PlatformCode.GOOGLE, "123")) is None


@pytest.mark.parametrize("raw", ["token", "{}", "[]", '{"connected_account_id":"ca_wrong"}'])
def test_malformed_binding_fails_closed(tmp_path, raw):
    encrypted, reader, ref, record = setup_store(tmp_path)
    encrypted.save_credential(ref, replace(record, token=raw))
    assert asyncio.run(reader.get_credential(PlatformCode.GOOGLE, "123")) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "ca_other"),
        ("user_id", "another-user"),
        ("status", "REVOKED"),
        ("status", "INACTIVE"),
        ("toolkit", {"slug": "gmail"}),
        ("auth_config", {"id": "ac_other"}),
        ("user_id", None),
        ("is_disabled", True),
        ("auth_config", {"id": "ac_bound", "is_disabled": True}),
    ],
)
def test_remote_connection_mismatch_never_reaches_proxy(tmp_path, field, value):
    _, reader, _, _ = setup_store(tmp_path)
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={**metadata(), field: value})

    with pytest.raises(CredentialNotConnectedError, match="not_verified"):
        request(transport(reader, handler))
    assert len(calls) == 1


def test_revoke_while_remote_metadata_inflight_prevents_proxy(tmp_path):
    encrypted, reader, ref, _ = setup_store(tmp_path)
    calls = []

    def handler(req):
        calls.append(req)
        encrypted.revoke_credential(ref, at=NOW)
        return httpx.Response(200, json=metadata())

    with pytest.raises(CredentialNotConnectedError):
        request(transport(reader, handler))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "endpoint,method",
    [
        ("https://evil.invalid/", "POST"),
        ("//evil.invalid", "GET"),
        ("/v25/customers/999/googleAds:mutate", "POST"),
        ("/v24/customers/123/googleAds:mutate", "POST"),
        ("/v25/customers/123/googleAds:mutate?x=1", "POST"),
        ("/v25/customers/123/googleAds:mutate", "DELETE"),
        ("/v25/customers/123/../../tokens", "POST"),
    ],
)
def test_unrecognized_endpoint_fails_before_http(tmp_path, endpoint, method):
    _, reader, _, _ = setup_store(tmp_path)

    def forbidden(req):
        pytest.fail("must not access network")

    with pytest.raises(ComposioTransportError, match="endpoint_not_supported"):
        request(transport(reader, forbidden), endpoint=endpoint, method=method)


@pytest.mark.parametrize("failure", ["timeout", "redirect", "quota", "body_error", "invalid_json"])
def test_proxy_failure_is_redacted_and_never_retried(tmp_path, failure):
    _, reader, _, _ = setup_store(tmp_path)
    calls = []

    def handler(req):
        calls.append(req)
        if req.method == "GET":
            return httpx.Response(200, json=metadata())
        if failure == "timeout":
            raise httpx.ReadTimeout("private-broker-key secret upstream body", request=req)
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil.invalid/"})
        if failure == "quota":
            return httpx.Response(429, json={"error": "private-broker-key"})
        if failure == "body_error":
            return httpx.Response(
                200, json={"status": 200, "data": {"error": "private-broker-key"}}
            )
        return httpx.Response(200, content=b"secret invalid response")

    with pytest.raises(ComposioTransportError) as caught:
        request(transport(reader, handler))
    assert "private-broker-key" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert len(calls) == 2


def test_google_real_sdk_protos_preserve_micros_mask_and_enum(tmp_path):
    _, reader, _, _ = setup_store(tmp_path)
    payloads = []

    def handler(req):
        assert req.url.host == "backend.composio.dev"
        assert req.headers["x-api-key"] == "private-broker-key"
        if req.method == "GET":
            return httpx.Response(200, json=metadata())
        assert req.url.path == "/api/v3.1/tools/execute/proxy"
        payload = json.loads(req.content)
        payloads.append(payload)
        assert payload["connected_account_id"] == "ca_bound"
        assert payload["parameters"] == [
            {"name": "login-customer-id", "value": "999", "type": "header"}
        ]
        if payload["endpoint"].endswith("searchStream"):
            data = [
                {
                    "results": [
                        {
                            "campaign": {"status": "PAUSED", "id": "456"},
                            "customer": {"currencyCode": "EUR"},
                        }
                    ]
                }
            ]
        else:
            data = {"results": [{"resourceName": "customers/123/campaignBudgets/456"}]}
        return httpx.Response(200, json={"status": 200, "data": data})

    client = ComposioGoogleAdsSearchClient(
        client_id="",
        client_secret="",
        credential_store=reader,
        composio_transport=transport(reader, handler),
    )
    rows = list(client.search_stream("123", "SELECT campaign.id, campaign.status FROM campaign"))
    assert rows == [{"campaign.id": 456, "campaign.status": "PAUSED"}]
    assert (
        client.mutate_campaign_budget("123", "customers/123/campaignBudgets/456", 10_000_000)
        == "customers/123/campaignBudgets/456"
    )
    operation = payloads[-1]["body"]["operations"][0]
    assert operation == {
        "update": {"resourceName": "customers/123/campaignBudgets/456", "amountMicros": "10000000"},
        "updateMask": "resourceName,amountMicros",
    }
    assert "customerId" not in payloads[-1]["body"]


@pytest.mark.parametrize(
    "kind,expected",
    [
        (EntityLevel.CAMPAIGN, "campaigns:mutate"),
        (EntityLevel.AD_SET, "adGroups:mutate"),
        (EntityLevel.AD, "adGroupAds:mutate"),
    ],
)
def test_google_status_reuses_native_sdk_operation(tmp_path, kind, expected):
    _, reader, _, _ = setup_store(tmp_path)
    resource = "customers/123/campaigns/456"

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata())
        payload = json.loads(req.content)
        assert payload["endpoint"].endswith(expected)
        assert payload["body"]["operations"][0]["update"]["status"] == "PAUSED"
        return httpx.Response(
            200, json={"status": 200, "data": {"results": [{"resourceName": resource}]}}
        )

    client = ComposioGoogleAdsSearchClient(
        client_id="",
        client_secret="",
        credential_store=reader,
        composio_transport=transport(reader, handler),
    )
    assert client.mutate_status("123", resource, kind, "PAUSED") == resource


@pytest.mark.parametrize("wrong_owner", [False, True])
def test_meta_owner_check_still_precedes_budget_write(tmp_path, wrong_owner):
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)
    payloads = []

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(PlatformCode.META))
        payload = json.loads(req.content)
        payloads.append(payload)
        assert payload["endpoint"] == "https://graph.facebook.com/v26.0/456"
        if payload["method"] == "GET":
            data = {"account_id": "999" if wrong_owner else "123"}
        else:
            assert payload["body"] == {"daily_budget": 1000}
            data = {"success": True}
        return httpx.Response(200, json={"status": 200, "data": data})

    client = ComposioMetaGraphClient(
        app_id="",
        app_secret="",
        credential_store=reader,
        composio_transport=transport(reader, handler),
    )
    if wrong_owner:
        with pytest.raises(CredentialNotConnectedError):
            client.update_node("act_123/456", {"daily_budget": 1000})
    else:
        client.update_node("act_123/456", {"daily_budget": 1000})
    assert len(payloads) == (1 if wrong_owner else 2)


@pytest.mark.parametrize("platform", list(PlatformCode))
def test_creation_reuses_native_atomic_paused_confirmation(tmp_path, platform):
    _, reader, _, _ = setup_store(tmp_path, platform)
    writes = []

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(platform))
        payload = json.loads(req.content)
        if platform == PlatformCode.GOOGLE:
            writes.append(payload)
            budget, campaign = payload["body"]["mutateOperations"]
            b = budget["campaignBudgetOperation"]["create"]
            c = campaign["campaignOperation"]["create"]
            assert c["status"] == "PAUSED" and "manualCpc" in c
            assert b["amountMicros"] == "20000000"
            data = {
                "mutateOperationResponses": [
                    {
                        "campaignBudgetResult": {
                            "resourceName": "customers/123/campaignBudgets/111",
                            "campaignBudget": b,
                        }
                    },
                    {
                        "campaignResult": {
                            "resourceName": "customers/123/campaigns/456",
                            "campaign": {
                                **c,
                                "campaignBudget": "customers/123/campaignBudgets/111",
                            },
                        }
                    },
                ]
            }
        elif payload["method"] == "POST":
            writes.append(payload)
            assert payload["body"]["status"] == "PAUSED"
            assert payload["body"]["daily_budget"] == 2000
            data = {"id": "456"}
        else:
            data = {**writes[0]["body"], "account_id": "123"}
        return httpx.Response(200, json={"status": 200, "data": data})

    if platform == PlatformCode.GOOGLE:
        client = ComposioGoogleAdsSearchClient(
            client_id="",
            client_secret="",
            credential_store=reader,
            composio_transport=transport(reader, handler),
        )
        result = client.create_paused_campaign("123", creation_payload())
    else:
        client = ComposioMetaGraphClient(
            app_id="",
            app_secret="",
            credential_store=reader,
            composio_transport=transport(reader, handler),
        )
        result = client.create_paused_campaign("act_123", creation_payload("meta"))
    assert result["status"] == "PAUSED"
    assert result["daily_budget_minor"] == 2000
    assert len(writes) == 1


# ── Incidente 2026-09-15 (companion 0.2.26): lecturas de referencia por Composio ──
# `_check_endpoint` solo admitia `promote_pages` como arista GET de cuenta; las
# otras cinco herramientas de referencia de Meta y `KeywordPlanIdeaService` de
# Google morian aqui con `composio_endpoint_not_supported` mientras sus clientes
# nativos funcionaban. Lista blanca GET-only y cuenta exacta: sin POST nuevo.

_META_REFERENCE_EDGES = (
    "promote_pages",
    "adspixels",
    "customaudiences",
    "saved_audiences",
    "product_catalogs",
    "targetingsearch",
    "delivery_estimate",
)


@pytest.mark.parametrize("edge", _META_REFERENCE_EDGES)
def test_meta_reference_edges_are_admitted_get_only_for_the_bound_account(edge):
    _check_endpoint(PlatformCode.META, "act_123", f"/v26.0/act_123/{edge}", "GET")


@pytest.mark.parametrize(
    "account,endpoint,method",
    [
        ("act_123", "/v26.0/act_123/delivery_estimate", "POST"),
        ("act_123", "/v26.0/act_999/delivery_estimate", "GET"),
        ("act_123", "/v26.0/act_123/delivery_estimate?x=1", "GET"),
        ("act_123", "/v26.0/act_123/delivery_estimates", "GET"),
        ("123", "/v26.0/act_123/adspixels", "GET"),
    ],
)
def test_meta_reference_edges_reject_writes_other_accounts_and_near_misses(
    account, endpoint, method
):
    with pytest.raises(ComposioTransportError, match="endpoint_not_supported"):
        _check_endpoint(PlatformCode.META, account, endpoint, method)


def test_google_keyword_ideas_customer_level_method_is_admitted_for_the_bound_customer():
    _check_endpoint(PlatformCode.GOOGLE, "123", "/v25/customers/123:generateKeywordIdeas", "POST")


@pytest.mark.parametrize(
    "endpoint,method",
    [
        ("/v25/customers/999:generateKeywordIdeas", "POST"),
        ("/v25/customers/123:generateKeywordIdeas", "GET"),
        ("/v25/customers/123/:generateKeywordIdeas", "POST"),
        ("/v25/customers/123:generateKeywordHistoricalMetrics", "POST"),
    ],
)
def test_google_keyword_ideas_rejects_other_customers_methods_and_near_misses(endpoint, method):
    with pytest.raises(ComposioTransportError, match="endpoint_not_supported"):
        _check_endpoint(PlatformCode.GOOGLE, "123", endpoint, method)


def test_google_asset_mutate_is_admitted_for_the_bound_customer():
    _check_endpoint(PlatformCode.GOOGLE, "123", "/v25/customers/123/assets:mutate", "POST")


@pytest.mark.parametrize(
    "endpoint,method",
    [
        ("/v25/customers/999/assets:mutate", "POST"),
        ("/v25/customers/123/assets:mutate", "GET"),
        ("/v25/customers/123/assets:mutates", "POST"),
    ],
)
def test_google_asset_mutate_rejects_other_customers_methods_and_near_misses(endpoint, method):
    with pytest.raises(ComposioTransportError, match="endpoint_not_supported"):
        _check_endpoint(PlatformCode.GOOGLE, "123", endpoint, method)


def test_meta_creative_upload_is_admitted_post_only_for_the_bound_account():
    _check_endpoint(PlatformCode.META, "act_123", "/v26.0/act_123/adimages", "POST")


@pytest.mark.parametrize(
    "account,endpoint,method",
    [
        ("act_123", "/v26.0/act_999/adimages", "POST"),
        ("act_123", "/v26.0/act_123/adimages", "GET"),
        ("act_123", "/v26.0/act_123/advideos", "POST"),
        ("123", "/v26.0/act_123/adimages", "POST"),
    ],
)
def test_meta_creative_upload_rejects_other_accounts_reads_and_other_edges(
    account, endpoint, method
):
    with pytest.raises(ComposioTransportError, match="endpoint_not_supported"):
        _check_endpoint(PlatformCode.META, account, endpoint, method)


def test_meta_creative_upload_over_composio_sends_base64_bytes_and_name(tmp_path):
    """`_MetaApiFacade.call` (composio_sdk_clients.py) has no `files`
    parameter -- `create_image` must send the image inline as base64 in the
    JSON body, exactly like every other Composio-backed Meta write."""
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)
    payloads = []
    media = b"\x89PNG\r\n\x1a\nfake-bytes"

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(PlatformCode.META))
        payload = json.loads(req.content)
        payloads.append(payload)
        assert payload["endpoint"] == "https://graph.facebook.com/v26.0/act_123/adimages"
        data = {"images": {"photo.jpg": {"hash": "abc123hash", "url": "https://x/preview"}}}
        return httpx.Response(200, json={"status": 200, "data": data})

    client = ComposioMetaGraphClient(
        app_id="",
        app_secret="",
        credential_store=reader,
        composio_transport=transport(reader, handler),
    )

    result = client.create_image("act_123", "photo.jpg", media)

    body = payloads[0]["body"]
    assert body == {"bytes": base64.b64encode(media).decode("ascii"), "name": "photo.jpg"}
    assert result == {"hash": "abc123hash", "url": "https://x/preview"}


_ADS_ARCHIVE_QUERY = {
    "ad_reached_countries": "ES",
    "ad_active_status": "ACTIVE",
    "fields": "page_name,ad_creative_bodies",
    "limit": "50",
}


def test_meta_ads_archive_is_admitted_get_only_with_no_account_segment():
    _check_endpoint(
        PlatformCode.META, "act_123", "/v26.0/ads_archive", "GET", query=_ADS_ARCHIVE_QUERY
    )
    # No connected ad account backs this public-data search: any account
    # string reaches the same admission, since there is none to check.
    _check_endpoint(
        PlatformCode.META, "act_999", "/v26.0/ads_archive", "GET", query=_ADS_ARCHIVE_QUERY
    )


def test_meta_ads_archive_rejects_post():
    with pytest.raises(ComposioTransportError, match="endpoint_not_supported"):
        _check_endpoint(
            PlatformCode.META, "act_123", "/v26.0/ads_archive", "POST", query=_ADS_ARCHIVE_QUERY
        )


def test_meta_ads_archive_rejects_an_unknown_query_param():
    with pytest.raises(ComposioTransportError, match="endpoint_not_supported"):
        _check_endpoint(
            PlatformCode.META,
            "act_123",
            "/v26.0/ads_archive",
            "GET",
            query={**_ADS_ARCHIVE_QUERY, "access_token": "leak"},
        )


@pytest.mark.parametrize(
    "endpoint", ["/v26.0/ads_archive/", "/v26.0/act_123/ads_archive", "/v26.0/ads_archives"]
)
def test_meta_ads_archive_rejects_other_nodes(endpoint):
    with pytest.raises(ComposioTransportError, match="endpoint_not_supported"):
        _check_endpoint(PlatformCode.META, "act_123", endpoint, "GET", query=_ADS_ARCHIVE_QUERY)


def test_meta_ads_archive_rejects_a_missing_or_empty_query():
    with pytest.raises(ComposioTransportError, match="endpoint_not_supported"):
        _check_endpoint(PlatformCode.META, "act_123", "/v26.0/ads_archive", "GET")
    with pytest.raises(ComposioTransportError, match="endpoint_not_supported"):
        _check_endpoint(PlatformCode.META, "act_123", "/v26.0/ads_archive", "GET", query={})


# ── fix/ad-library-identity-reason: production evidence (16-sep, 0.2.32) --
# the broker logged `{"upstream_status":400,"upstream_error":"OAuthException/10/2332002"}`
# then answered `FAILED`/`adapter_error` generically. The transport now
# attaches that SAME sanitized triplet to `ComposioTransportError` itself,
# never a message/body/URL, so a caller can distinguish one documented
# upstream failure from another without this transport exposing more than
# it already logged.


def test_upstream_failure_carries_the_sanitized_status_and_error_code(tmp_path):
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(PlatformCode.META))
        return httpx.Response(
            200,
            json={
                "status": 400,
                "data": {
                    "error": {
                        "message": "private-broker-key secret upstream body",
                        "type": "OAuthException",
                        "code": 10,
                        "error_subcode": 2332002,
                        "error_user_title": "Authorization and login needed",
                    }
                },
            },
        )

    with pytest.raises(ComposioTransportError) as caught:
        request(
            transport(reader, handler),
            platform=PlatformCode.META,
            native_account_id="act_123",
            endpoint="/v26.0/ads_archive",
            method="GET",
            query=_ADS_ARCHIVE_QUERY,
            body=None,
        )

    assert caught.value.upstream_status == 400
    assert caught.value.upstream_error_code == "OAuthException/10/2332002"
    # Still nothing from the raw body crosses the boundary.
    assert "private-broker-key" not in str(caught.value)
    assert "message" not in str(caught.value)


def test_upstream_failure_without_a_recognizable_error_shape_leaves_the_code_none(tmp_path):
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(PlatformCode.META))
        return httpx.Response(200, json={"status": 404, "data": {"some": "unrecognized-shape"}})

    with pytest.raises(ComposioTransportError) as caught:
        request(
            transport(reader, handler),
            platform=PlatformCode.META,
            native_account_id="act_123",
            endpoint="/v26.0/ads_archive",
            method="GET",
            query=_ADS_ARCHIVE_QUERY,
            body=None,
        )

    assert caught.value.upstream_status == 404
    assert caught.value.upstream_error_code is None


# ── fix/ad-library-over-composio: `ComposioMetaAdLibraryClient` -- the node
# itself has no connected ad account (see `_check_endpoint` tests above),
# but the Composio proxy still needs one to authenticate: the caller's
# resolved `external_account_id`, one call at a time.


def test_composio_ad_library_client_sends_get_ads_archive_with_no_account_segment(tmp_path):
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)
    payloads = []

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(PlatformCode.META))
        payload = json.loads(req.content)
        payloads.append(payload)
        assert payload["endpoint"] == "https://graph.facebook.com/v26.0/ads_archive"
        return httpx.Response(
            200,
            json={
                "status": 200,
                "data": {"data": [{"page_name": "Acme", "ad_creative_bodies": ["Hola"]}]},
            },
        )

    client = ComposioMetaAdLibraryClient(composio_transport=transport(reader, handler))

    rows = client.search_ads_archive(
        country="ES",
        search_terms="zapatillas",
        search_page_ids="123456",
        active_status="ACTIVE",
        fields=("page_name", "ad_creative_bodies"),
        limit=50,
        external_account_id="act_123",
    )

    assert rows == [{"page_name": "Acme", "ad_creative_bodies": ["Hola"]}]
    assert len(payloads) == 1
    params = {p["name"]: p["value"] for p in payloads[0]["parameters"]}
    assert params == {
        "ad_reached_countries": '["ES"]',
        "ad_active_status": "ACTIVE",
        "fields": "page_name,ad_creative_bodies",
        "limit": "50",
        "search_terms": "zapatillas",
        "search_page_ids": '["123456"]',
    }


def test_composio_ad_library_client_omits_optional_filters_when_absent(tmp_path):
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(PlatformCode.META))
        payload = json.loads(req.content)
        params = {p["name"] for p in payload["parameters"]}
        assert "search_terms" not in params
        assert "search_page_ids" not in params
        return httpx.Response(200, json={"status": 200, "data": {"data": []}})

    client = ComposioMetaAdLibraryClient(composio_transport=transport(reader, handler))

    client.search_ads_archive(
        country="ES",
        search_terms=None,
        search_page_ids=None,
        active_status="ALL",
        fields=("page_name",),
        limit=50,
        external_account_id="act_123",
    )


def test_composio_ad_library_client_fails_closed_without_an_account(tmp_path):
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)

    def handler(req):
        raise AssertionError("no HTTP call expected without a resolved account")

    client = ComposioMetaAdLibraryClient(composio_transport=transport(reader, handler))

    with pytest.raises(CredentialNotConnectedError, match="account_missing"):
        client.search_ads_archive(
            country="ES",
            search_terms=None,
            search_page_ids=None,
            active_status="ACTIVE",
            fields=("page_name",),
            limit=50,
        )


def test_composio_ad_library_client_fails_closed_for_an_unbound_account(tmp_path):
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)

    def handler(req):
        raise AssertionError("no HTTP call expected before the binding check")

    client = ComposioMetaAdLibraryClient(composio_transport=transport(reader, handler))

    with pytest.raises(CredentialNotConnectedError):
        client.search_ads_archive(
            country="ES",
            search_terms=None,
            search_page_ids=None,
            active_status="ACTIVE",
            fields=("page_name",),
            limit=50,
            external_account_id="act_999",
        )


def test_composio_ad_library_client_rejects_a_response_without_a_valid_data_list(tmp_path):
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(PlatformCode.META))
        return httpx.Response(200, json={"status": 200, "data": {"data": "not-a-list"}})

    client = ComposioMetaAdLibraryClient(composio_transport=transport(reader, handler))

    with pytest.raises(ComposioTransportError, match="composio_meta_response_invalid"):
        client.search_ads_archive(
            country="ES",
            search_terms=None,
            search_page_ids=None,
            active_status="ACTIVE",
            fields=("page_name",),
            limit=50,
            external_account_id="act_123",
        )


# ── fix/ad-library-identity-reason: production evidence (16-sep, 0.2.32) --
# Meta's documented response when the Facebook user behind the token has
# not completed the Ad Library identity/location confirmation
# (facebook.com/ID): HTTP 400, `OAuthException`/10.


def test_composio_ad_library_client_raises_identity_required_for_the_documented_meta_error(
    tmp_path,
):
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(PlatformCode.META))
        return httpx.Response(
            200,
            json={
                "status": 400,
                "data": {
                    "error": {
                        "message": "private-broker-key secret upstream body",
                        "type": "OAuthException",
                        "code": 10,
                        "error_subcode": 2332002,
                        "error_user_title": "Authorization and login needed",
                    }
                },
            },
        )

    client = ComposioMetaAdLibraryClient(composio_transport=transport(reader, handler))

    with pytest.raises(MetaAdLibraryIdentityRequiredError) as caught:
        client.search_ads_archive(
            country="ES",
            search_terms=None,
            search_page_ids=None,
            active_status="ACTIVE",
            fields=("page_name",),
            limit=50,
            external_account_id="act_123",
        )

    # Never the provider's raw message, only the static reason this module names.
    assert "private-broker-key" not in str(caught.value)
    assert "message" not in str(caught.value)


@pytest.mark.parametrize(
    ("upstream_status", "error_shape"),
    [
        (400, {"type": "OAuthException", "code": 190}),  # different code: expired token
        (400, {"type": "OAuthException", "code": 10}),  # code 10 WITHOUT the identity subcode
        (400, {"type": "OAuthException", "code": 10, "error_subcode": 1}),  # other subcode
        (403, {"type": "OAuthException", "code": 10}),  # right code, different status
        (500, {"type": "OAuthException", "code": 10}),  # right code, different status
    ],
)
def test_composio_ad_library_client_keeps_any_other_meta_error_generic(
    tmp_path, upstream_status, error_shape
):
    """Only the ONE documented signal (400 + `OAuthException/10`) becomes
    `MetaAdLibraryIdentityRequiredError` -- everything else, including a
    near-miss on either status or code alone, stays the honest generic
    `ComposioTransportError` (mapped to `provider_error` further up)."""
    _, reader, _, _ = setup_store(tmp_path, PlatformCode.META)

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json=metadata(PlatformCode.META))
        return httpx.Response(
            200, json={"status": upstream_status, "data": {"error": error_shape}}
        )

    client = ComposioMetaAdLibraryClient(composio_transport=transport(reader, handler))

    with pytest.raises(ComposioTransportError) as caught:
        client.search_ads_archive(
            country="ES",
            search_terms=None,
            search_page_ids=None,
            active_status="ACTIVE",
            fields=("page_name",),
            limit=50,
            external_account_id="act_123",
        )

    assert not isinstance(caught.value, MetaAdLibraryIdentityRequiredError)
