from __future__ import annotations

from typing import Any

import httpx
import pytest

from safent_ads.broker.application.ports import PlatformCredential
from safent_ads.broker.platforms.errors import CredentialNotConnectedError
from safent_ads.broker.platforms.google_tag_manager import LiveGoogleTagManagerClient
from safent_ads.shared.ids import PlatformCode

_READ = "https://www.googleapis.com/auth/tagmanager.readonly"
_PUBLISH = "https://www.googleapis.com/auth/tagmanager.publish"


class _Credentials:
    def __init__(self, scopes: tuple[str, ...]) -> None:
        self._scopes = scopes

    async def get_credential(
        self, platform: PlatformCode, external_account_id: str
    ) -> PlatformCredential | None:
        return PlatformCredential(
            platform=platform,
            external_account_id=external_account_id,
            refresh_token="refresh",
            scopes=self._scopes,
        )


class _Http:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def __aenter__(self) -> _Http:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        self.requests.append(("POST", url, kwargs))
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"access_token": "access"})

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,  # noqa: ANN401
    ) -> httpx.Response:
        self.requests.append((method, url, kwargs))
        request = httpx.Request(method, url)
        body = (
            {"account": [{"path": "accounts/1", "name": "Test"}]}
            if method == "GET"
            else {"path": "accounts/1/containers/2/versions/7"}
        )
        return httpx.Response(200, request=request, json=body)


async def test_lists_accounts_with_scoped_google_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _Http()
    monkeypatch.setattr(
        "safent_ads.broker.platforms.google_tag_manager.build_guarded_async_client",
        lambda **_kwargs: http,
    )
    client = LiveGoogleTagManagerClient(
        client_id="client",
        client_secret="secret",
        credential_store=_Credentials((_READ,)),
    )

    result = await client.read("1234567890", resource="accounts", parent_path=None)

    assert result["account"][0]["path"] == "accounts/1"
    assert http.requests[-1][1].endswith("/tagmanager/v2/accounts")


async def test_publish_uses_distinct_publish_scope_and_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _Http()
    monkeypatch.setattr(
        "safent_ads.broker.platforms.google_tag_manager.build_guarded_async_client",
        lambda **_kwargs: http,
    )
    client = LiveGoogleTagManagerClient(
        client_id="client",
        client_secret="secret",
        credential_store=_Credentials((_PUBLISH,)),
    )

    await client.apply_change(
        "1234567890",
        {
            "action": "publish_version",
            "resource_path": "accounts/1/containers/2/versions/7",
            "fingerprint": "fp-7",
        },
    )

    method, url, kwargs = http.requests[-1]
    assert method == "POST"
    assert url.endswith("/accounts/1/containers/2/versions/7:publish")
    assert kwargs["params"] == {"fingerprint": "fp-7"}


async def test_missing_gtm_scope_requires_reconnect() -> None:
    client = LiveGoogleTagManagerClient(
        client_id="client",
        client_secret="secret",
        credential_store=_Credentials(("https://www.googleapis.com/auth/adwords",)),
    )

    with pytest.raises(CredentialNotConnectedError):
        await client.read("1234567890", resource="accounts", parent_path=None)
