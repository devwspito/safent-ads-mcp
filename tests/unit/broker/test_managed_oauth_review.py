"""Independent negative admission checks for managed OAuth, no live credentials."""

from datetime import UTC, datetime, timedelta

import pytest

from safent_ads.broker.application.errors import OAuthProviderDeniedError
from safent_ads.broker.application.managed_oauth_connect import (
    ManagedOAuthConfig,
    ManagedOAuthConnectService,
)
from safent_ads.broker.application.ports import ComposioAccountBinding, ConnectSessionRecord
from safent_ads.shared.ids import PlatformCode


class Clock:
    def now(self):
        return datetime(2026, 9, 13, tzinfo=UTC)


class Store:
    def __init__(self):
        self.records = {}
        self.bindings = []

    def save_credential(self, ref, record):
        self.records[ref] = record

    def bind_account_credential(self, platform, account_id, ref, **scope):
        self.bindings.append((platform, account_id, ref, scope))


class Http:
    def __init__(self, *, disabled=False, bad_manager=False):
        self.disabled = disabled
        self.bad_manager = bad_manager
        self.customer_queries = []

    async def get_json(self, url, **kwargs):
        assert kwargs["headers"] == {"x-api-key": "synthetic-api-key"}
        if "/auth_configs/" in url:
            return {
                "id": "ac_review",
                "status": "ENABLED",
                "auth_scheme": "OAUTH2",
                "toolkit": {"slug": "googleads"},
            }
        return {
            "id": "ca_review",
            "status": "ACTIVE",
            "user_id": "review-user",
            "toolkit": {"slug": "googleads"},
            "auth_config": {"id": "ac_review"},
            "is_disabled": self.disabled,
        }

    async def post_json(self, url, *, json_body, **kwargs):
        assert url == "https://backend.composio.dev/api/v3.1/tools/execute/proxy"
        assert kwargs["headers"] == {"x-api-key": "synthetic-api-key"}
        endpoint = json_body["endpoint"]
        query = json_body["body"]["query"]
        self.customer_queries.append((endpoint, query))
        if "FROM customer_client" in query:
            children = [
                {
                    "customerClient": {
                        "id": "2222222222",
                        "currencyCode": "EUR",
                        "timeZone": "Europe/Madrid",
                    }
                }
            ]
            if self.bad_manager:
                children.append(
                    {
                        "customerClient": {
                            "id": "9" * 21,
                            "currencyCode": "EUR",
                            "timeZone": "Europe/Madrid",
                        }
                    }
                )
            return {
                "status": 200,
                "data": {"results": children},
            }
        account_id = endpoint.split("/customers/")[1].split("/")[0]
        return {
            "status": 200,
            "data": {
                "results": [
                    {
                        "customer": {
                            "id": account_id,
                            "manager": True,
                            "currencyCode": "EUR",
                            "timeZone": "Europe/Madrid",
                        }
                    }
                ]
            },
        }


@pytest.mark.asyncio
async def test_explicitly_disabled_remote_connection_is_not_verified():
    service = ManagedOAuthConnectService(
        ManagedOAuthConfig("synthetic-api-key", google_auth_config_id="ac_review"),
        Http(disabled=True),
        Store(),
        Clock(),
    )
    with pytest.raises(OAuthProviderDeniedError):
        await service._verify_binding(
            PlatformCode.GOOGLE,
            ComposioAccountBinding("ca_review", "review-user", "ac_review"),
        )


@pytest.mark.asyncio
async def test_later_invalid_manager_binding_does_not_leave_earlier_account_admitted():
    store = Store()
    http = Http(bad_manager=True)
    service = ManagedOAuthConnectService(
        ManagedOAuthConfig("synthetic-api-key", google_auth_config_id="ac_review"),
        http,
        store,
        Clock(),
    )
    now = Clock().now()
    session = ConnectSessionRecord(
        provider=PlatformCode.GOOGLE,
        business_id="00000000-0000-4000-8000-000000000001",
        redirect_uri="http://127.0.0.1:33015/callback",
        pkce_verifier=None,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
        connection_id="00000000-0000-4000-8000-000000000002",
        owner_id="00000000-0000-4000-8000-000000000003",
        managed_connection_id="ca_review",
        managed_user_id="review-user",
        managed_auth_config_id="ac_review",
        google_customer_id="1111111111",
    )
    with pytest.raises(OAuthProviderDeniedError, match="managed_account_discovery_invalid"):
        await service.complete(session, connected_account_id="ca_review")
    assert len(http.customer_queries) == 2
    assert all(
        endpoint == "https://googleads.googleapis.com/v25/customers/1111111111/googleAds:search"
        for endpoint, _query in http.customer_queries
    )
    assert "FROM customer_client" in http.customer_queries[-1][1]
    assert store.records == {}, "Do not persist the first account before validating every binding"
    assert store.bindings == []
