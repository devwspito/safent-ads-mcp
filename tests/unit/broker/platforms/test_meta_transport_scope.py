"""Real SDK wrapper, mocked network: account isolation and complete pagination."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from safent_ads.broker.application.ports import PlatformCredential
from safent_ads.broker.infrastructure.in_memory_credential_store import InMemoryCredentialStore
from safent_ads.broker.platforms import live_meta_graph_client as module
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.platforms.live_meta_graph_client import LiveMetaGraphClient
from safent_ads.shared.ids import PlatformCode


def _client() -> LiveMetaGraphClient:
    credentials = {
        (PlatformCode.META, f"act_{account}"): PlatformCredential(
            platform=PlatformCode.META,
            external_account_id=f"act_{account}",
            access_token=f"token-{account}",
        )
        for account in (123, 456)
    }
    return LiveMetaGraphClient(
        app_id="test",
        app_secret="test",
        credential_store=InMemoryCredentialStore(credentials),
    )


class _Api:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, list[str], dict[str, Any]]] = []

    def call(self, method: str, path: list[str], *, params: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((method, path, dict(params)))
        response = self.responses.pop(0)
        return SimpleNamespace(json=lambda: response)


def test_nested_node_needs_no_prior_inventory_and_has_no_cross_account_cache() -> None:
    client = _client()
    references = ["act_123/11", "act_456/22"] * 50
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(client._resolve_credential, references))
    assert [credential.access_token for credential in results] == ["token-123", "token-456"] * 50
    with pytest.raises(CredentialNotConnectedError):
        client._resolve_credential("11")


def test_sdk_sessions_are_not_global(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("global SDK initialization is forbidden")

    monkeypatch.setattr(module.FacebookAdsApi, "init", forbidden)
    client = _client()
    first = client._api_for("act_123/11")
    second = client._api_for("act_456/22")
    assert first is not second
    assert first._session.access_token == "token-123"  # noqa: S105 - test fixture
    assert second._session.access_token == "token-456"  # noqa: S105 - test fixture


def test_pagination_keeps_endpoint_and_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api(
        [
            {
                "data": [{"id": "11"}],
                "paging": {"next": "https://untrusted.invalid/", "cursors": {"after": "cursor"}},
            },
            {"data": [{"id": "22"}]},
        ]
    )
    client = _client()
    monkeypatch.setattr(client, "_api_for", lambda _: api)
    rows = client.get_edge("act_123", "campaigns", ("id",), {"limit": 1})
    assert [row["id"] for row in rows] == ["11", "22"]
    assert api.calls[1] == (
        "GET",
        ["act_123", "campaigns"],
        {"fields": "id", "limit": 1, "after": "cursor"},
    )


@pytest.mark.parametrize("cursor", [None, "repeated"])
def test_invalid_cursor_fails_instead_of_returning_partial_data(
    monkeypatch: pytest.MonkeyPatch,
    cursor: str | None,
) -> None:
    page = {"data": [], "paging": {"next": "next", "cursors": {"after": cursor}}}
    api = _Api([page, page])
    client = _client()
    monkeypatch.setattr(client, "_api_for", lambda _: api)
    with pytest.raises(ValueError, match="cursor"):
        client.get_edge("act_123", "campaigns", ("id",))


@pytest.mark.parametrize("operation", ["read", "edge", "write"])
def test_wrong_owner_is_rejected_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    api = _Api([{"id": "11", "account_id": "456"}])
    client = _client()
    monkeypatch.setattr(client, "_api_for", lambda _: api)
    with pytest.raises(CredentialNotConnectedError, match="pertenece"):
        if operation == "read":
            client.get_node("act_123/11", ("id",))
        elif operation == "edge":
            client.get_edge("act_123/11", "adsets", ("id",))
        else:
            client.update_node("act_123/11", {"status": "PAUSED"})
    assert all(method == "GET" for method, _, _ in api.calls)


def test_update_requires_provider_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _Api([{"account_id": "123"}, {"success": False}])
    client = _client()
    monkeypatch.setattr(client, "_api_for", lambda _: api)
    with pytest.raises(ValueError, match="confirmo"):
        client.update_node("act_123/11", {"status": "PAUSED"})
