from __future__ import annotations

# ruff: noqa: S105 - credential kind literals in assertions are not secrets.
import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from safent_ads.broker.application.errors import (
    GoogleAccountAccessDeniedError,
    GoogleAccountNotEnabledError,
    GoogleAccountSelectionRequiredError,
    OAuthProviderDeniedError,
    OAuthSessionExpiredError,
    OAuthSessionNotFoundError,
)
from safent_ads.broker.application.managed_oauth_connect import (
    ManagedOAuthConfig,
    ManagedOAuthConnectService,
)
from safent_ads.broker.application.oauth_connect_flow import OAuthConnectFlow
from safent_ads.broker.infrastructure.credential_store import EncryptedCredentialStore
from safent_ads.shared.ids import PlatformCode


class Cloud:
    def __init__(self):
        self.calls, self.links = [], []
        self.config_status, self.status = "ENABLED", "ACTIVE"
        self.toolkit, self.remote_user = None, None
        self.redirect = "https://connect.composio.dev/link-test"
        self.permissions = ["ads_management", "ads_read"]
        self.google_id = "1234567890"
        self.google_redacted = False
        self.google_error = None
        self.malformed_account = False
        self.meta_paging = False
        self.manager = False

    async def get_json(self, url, *, headers=None, params=None):
        assert headers == {"x-api-key": "test-private-key"}
        assert params is None
        self.calls.append(("GET", url, None))
        if "/auth_configs/" in url:
            config_id = url.rsplit("/", 1)[1]
            return {
                "id": config_id,
                "status": self.config_status,
                "auth_scheme": "OAUTH2",
                "toolkit": {"slug": "googleads" if "google" in config_id else "metaads"},
            }
        link = self.links[-1]
        return {
            "id": "ca_connected",
            "status": self.status,
            "user_id": self.remote_user or link["user_id"],
            "toolkit": {
                "slug": self.toolkit
                or ("googleads" if "google" in link["auth_config_id"] else "metaads")
            },
            "auth_config": {"id": link["auth_config_id"]},
        }

    async def post_json(self, url, *, json_body, headers=None):
        assert headers == {"x-api-key": "test-private-key"}
        self.calls.append(("POST", url, json_body))
        if url.endswith("/connected_accounts/link"):
            self.links.append(json_body)
            return {"connected_account_id": "ca_connected", "redirect_url": self.redirect}
        endpoint = json_body["endpoint"]
        if endpoint.endswith("customers:listAccessibleCustomers"):
            raise AssertionError("Do not import unrelated Google accounts")
        elif endpoint.endswith("googleAds:search"):
            if self.google_error:
                return {"status": 403, "data": {"error": {"details": [{"errors": [
                    {"errorCode": {"authorizationError": self.google_error}}
                ]}]}}}
            if "FROM customer_client" in json_body["body"]["query"]:
                data = {
                    "results": [
                        {
                            "customerClient": {
                                "id": "456",
                                "currencyCode": "EUR",
                                "timeZone": "Europe/Madrid",
                            }
                        }
                    ]
                }
            else:
                data = {
                    "results": [
                        {
                            "customer": {
                                "id": "***REDACTED***" if self.google_redacted else self.google_id,
                                "resourceName": (
                                    "customers/***REDACTED***" if self.google_redacted
                                    else f"customers/{self.google_id}"
                                ),
                                "currencyCode": "EUR",
                                "timeZone": "Europe/Madrid",
                                "manager": self.manager,
                            }
                        }
                    ]
                }
        elif endpoint.endswith("me/permissions"):
            data = {"data": [{"permission": p, "status": "granted"} for p in self.permissions]}
        elif endpoint.endswith("me/adaccounts"):
            data = {
                "data": [{"id": "act_789", "currency": "EUR", "timezone_name": "Europe/Madrid"}]
            }
            if self.malformed_account:
                data["data"].append({"id": "act_888", "currency": "EUR"})
            if self.meta_paging:
                data["paging"] = {
                    "next": "https://evil.example/private",
                    "cursors": {"after": "cursor"},
                }
        else:
            raise AssertionError(f"Unexpected endpoint {endpoint}")
        return {"status": 200, "data": data}


@pytest.fixture
def setup(tmp_path):
    cloud = Cloud()
    now = [datetime(2026, 9, 13, tzinfo=UTC)]
    clock = SimpleNamespace(now=lambda: now[0])
    store = EncryptedCredentialStore(tmp_path, base64.b64encode(b"x" * 32).decode())
    config = ManagedOAuthConfig("test-private-key", "ac_google", "ac_meta")
    managed = ManagedOAuthConnectService(config, cloud, store, clock)
    google, meta = Mock(), Mock()
    flow = OAuthConnectFlow(store, google, meta, clock, managed=managed)
    return SimpleNamespace(
        cloud=cloud,
        store=store,
        flow=flow,
        managed=managed,
        clock=clock,
        now=now,
        google=google,
        meta=meta,
        root=tmp_path,
        config=config,
    )


async def begin(s, platform=PlatformCode.GOOGLE, **overrides):
    params = {
        "provider": platform,
        "business_id": str(uuid.uuid4()),
        "owner_id": str(uuid.uuid4()),
        "redirect_uri": f"http://127.0.0.1:33015/ads/api/v1/platform-accounts/{platform.value}/reconnect/callback",
    }
    if platform == PlatformCode.GOOGLE:
        params["google_customer_id"] = "1234567890"
    params.update(overrides)
    return await s.flow.begin(**params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,external", [(PlatformCode.GOOGLE, "1234567890"), (PlatformCode.META, "act_789")]
)
async def test_real_flow_binds_verified_discovery_without_tokens(setup, platform, external):
    start = await begin(setup, platform)
    callback = setup.cloud.links[-1]["callback_url"]
    assert parse_qs(urlsplit(callback).query) == {"state": [start.state], "managed": ["1"]}
    # Simulate process restart: session binding must survive encrypted persistence.
    second = OAuthConnectFlow(
        setup.store, setup.google, setup.meta, setup.clock, managed=setup.managed
    )
    result = await second.complete(state=start.state, code="ca_connected")
    account = result.accounts[0]
    assert account.external_account_id == external
    assert account.connection_id == start.connection_id
    record = setup.store.get_credential(account.credential_ref_id)
    assert record.token_type == "composio_connection"
    assert json.loads(record.token)["connected_account_id"] == "ca_connected"
    assert "access_token" not in record.token and "refresh_token" not in record.token
    assert "test-private-key" not in repr(result) + repr(record) + repr(setup.config)
    assert (
        setup.store.account_credential_ref(
            platform, external, business_id=account.business_id, connection_id=account.connection_id
        )
        == account.credential_ref_id
    )
    assert not setup.google.mock_calls and not setup.meta.mock_calls
    with pytest.raises(OAuthSessionNotFoundError):
        await second.complete(state=start.state, code="ca_connected")


@pytest.mark.asyncio
async def test_each_attempt_has_distinct_server_user_even_same_owner(setup):
    owner, business = str(uuid.uuid4()), str(uuid.uuid4())
    await begin(setup, owner_id=owner, business_id=business)
    await begin(setup, owner_id=owner, business_id=business)
    assert setup.cloud.links[0]["user_id"] != setup.cloud.links[1]["user_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "INITIATED"),
        ("status", "DISABLED"),
        ("remote_user", "another-tenant"),
        ("toolkit", "gmail"),
        ("config_status", "DISABLED"),
    ],
)
async def test_invalid_remote_identity_saves_nothing(setup, field, value):
    start = await begin(setup)
    setattr(setup.cloud, field, value)
    with pytest.raises(OAuthProviderDeniedError):
        await setup.flow.complete(state=start.state, code="ca_connected")
    assert not list((setup.root / "credentials").iterdir())


@pytest.mark.asyncio
async def test_callback_cannot_choose_connected_account(setup):
    start = await begin(setup)
    count = len(setup.cloud.calls)
    with pytest.raises(OAuthProviderDeniedError, match="mismatch"):
        await setup.flow.complete(state=start.state, code="ca_someone_else")
    assert len(setup.cloud.calls) == count


@pytest.mark.asyncio
async def test_expired_and_concurrent_replay_are_rejected(setup):
    start = await begin(setup)
    results = await asyncio.gather(
        setup.flow.complete(state=start.state, code="ca_connected"),
        setup.flow.complete(state=start.state, code="ca_connected"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, OAuthSessionNotFoundError) for r in results) == 1
    start = await begin(setup)
    setup.now[0] += timedelta(minutes=11)
    with pytest.raises(OAuthSessionExpiredError):
        await setup.flow.complete(state=start.state, code="ca_connected")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://connect.composio.dev/x",
        "https://evil.example/x",
        "https://connect.composio.dev@evil.example/x",
        "https://secret@connect.composio.dev/x",
    ],
)
async def test_unsafe_connect_urls_rejected(setup, url):
    setup.cloud.redirect = url
    with pytest.raises(OAuthProviderDeniedError, match="link_invalid"):
        await begin(setup)
    assert not list((setup.root / "sessions").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/callback",
        "https://secret@example.com/callback",
        "https://example.com/callback?state=attacker",
    ],
)
async def test_callback_origin_is_checked_before_link(setup, url):
    with pytest.raises(OAuthProviderDeniedError, match="callback_invalid"):
        await begin(setup, redirect_uri=url)
    assert not setup.cloud.links


@pytest.mark.asyncio
async def test_google_manager_discovers_child_and_binds_correct_login_customer(setup):
    setup.cloud.manager = True
    start = await begin(setup)
    result = await setup.flow.complete(state=start.state, code="ca_connected")
    assert [a.external_account_id for a in result.accounts] == ["456"]
    record = setup.store.get_credential(result.accounts[0].credential_ref_id)
    assert json.loads(record.token)["login_customer_id"] == "1234567890"


@pytest.mark.asyncio
async def test_selected_customer_prefills_hosted_link_and_ignores_other_accounts(setup):
    start = await begin(setup, google_customer_id="123-456-7890")
    assert setup.cloud.links[0]["connection_data"] == {"generic_id": "1234567890"}
    result = await setup.flow.complete(state=start.state, code="ca_connected")
    assert [a.external_account_id for a in result.accounts] == ["1234567890"]
    endpoints = [body.get("endpoint", "") for _, _, body in setup.cloud.calls if body]
    assert not any("listAccessibleCustomers" in value for value in endpoints)
    assert any("customers/1234567890/googleAds:search" in value for value in endpoints)


@pytest.mark.asyncio
async def test_managed_google_requires_number_before_creating_remote_link(setup):
    with pytest.raises(GoogleAccountSelectionRequiredError):
        await begin(setup, google_customer_id=None)
    assert setup.cloud.links == []


@pytest.mark.asyncio
async def test_redacted_nonsecret_customer_id_is_bound_to_known_authenticated_path(setup):
    setup.cloud.google_redacted = True
    start = await begin(setup)
    result = await setup.flow.complete(state=start.state, code="ca_connected")
    assert result.accounts[0].external_account_id == "1234567890"


@pytest.mark.asyncio
@pytest.mark.parametrize("code,error", [
    ("CUSTOMER_NOT_ENABLED", GoogleAccountNotEnabledError),
    ("USER_PERMISSION_DENIED", GoogleAccountAccessDeniedError),
    ("UNKNOWN", OAuthProviderDeniedError),
])
async def test_selected_account_error_is_not_reported_as_failed_consent(setup, code, error):
    setup.cloud.google_error = code
    start = await begin(setup)
    with pytest.raises(error):
        await setup.flow.complete(state=start.state, code="ca_connected")
    assert not list((setup.root / "credentials").iterdir())


@pytest.mark.asyncio
async def test_redacted_id_with_conflicting_resource_is_not_admitted(setup):
    setup.cloud.google_redacted = True
    original = setup.cloud.post_json
    async def conflicting(url, **kwargs):
        result = await original(url, **kwargs)
        for row in result.get("data", {}).get("results", []):
            row["customer"]["resourceName"] = "customers/6666666666"
        return result
    setup.cloud.post_json = conflicting
    start = await begin(setup)
    with pytest.raises(OAuthProviderDeniedError):
        await setup.flow.complete(state=start.state, code="ca_connected")
    assert not list((setup.root / "credentials").iterdir())


@pytest.mark.asyncio
async def test_google_metadata_cannot_change_identity(setup):
    start = await begin(setup)
    setup.cloud.google_id = "666"
    with pytest.raises(OAuthProviderDeniedError):
        await setup.flow.complete(state=start.state, code="ca_connected")
    assert not list((setup.root / "credentials").iterdir())


@pytest.mark.asyncio
async def test_meta_read_only_scope_is_not_presented_as_full_access(setup):
    setup.cloud.permissions = ["ads_read"]
    start = await begin(setup, PlatformCode.META)
    with pytest.raises(OAuthProviderDeniedError, match="write_permission_missing"):
        await setup.flow.complete(state=start.state, code="ca_connected")
    assert not list((setup.root / "credentials").iterdir())


@pytest.mark.asyncio
async def test_inventory_all_or_nothing(setup):
    setup.cloud.malformed_account = True
    start = await begin(setup, PlatformCode.META)
    with pytest.raises(OAuthProviderDeniedError):
        await setup.flow.complete(state=start.state, code="ca_connected")
    assert not list((setup.root / "credentials").iterdir())


@pytest.mark.asyncio
async def test_meta_paging_never_follows_untrusted_url_and_cycle_is_bounded(setup):
    setup.cloud.meta_paging = True
    start = await begin(setup, PlatformCode.META)
    with pytest.raises(OAuthProviderDeniedError):
        await setup.flow.complete(state=start.state, code="ca_connected")
    assert not list((setup.root / "credentials").iterdir())
    assert all("evil.example" not in url for _, url, _ in setup.cloud.calls)
