from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from safent_ads.accounts.application.ports import AccountRef
from safent_ads.broker.presentation.dispatcher import BrokerRuntime, handle_payload
from safent_ads.shared.ids import PlatformCode

_BUSINESS_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_CONNECTION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[tuple[AccountRef, str, str | None]] = []

    async def read_google_tag_manager(
        self,
        account_ref: AccountRef,
        *,
        resource: str,
        parent_path: str | None,
    ) -> dict[str, Any]:
        self.calls.append((account_ref, resource, parent_path))
        return {"container": [{"path": "accounts/1/containers/2"}]}


class _Adapters:
    def __init__(self, adapter: _Adapter) -> None:
        self._adapter = adapter

    def get(self, platform: PlatformCode) -> _Adapter:
        assert platform is PlatformCode.GOOGLE
        return self._adapter


async def test_dispatches_scoped_google_tag_manager_read() -> None:
    adapter = _Adapter()
    runtime = BrokerRuntime(
        adapters=_Adapters(adapter),  # type: ignore[arg-type]
        oauth_flow=SimpleNamespace(),  # type: ignore[arg-type]
        app_credentials=SimpleNamespace(),  # type: ignore[arg-type]
    )
    payload = json.dumps(
        {
            "op": "google_tag_manager_read",
            "platform": "google",
            "external_account_id": "1234567890",
            "business_id": _BUSINESS_ID,
            "connection_id": _CONNECTION_ID,
            "resource": "containers",
            "parent_path": "accounts/1",
        }
    ).encode()

    response = json.loads(await handle_payload(payload, runtime))

    assert response == {
        "ok": True,
        "result": {"container": [{"path": "accounts/1/containers/2"}]},
    }
    account_ref, resource, parent_path = adapter.calls[0]
    assert account_ref.external_account_id == "1234567890"
    assert str(account_ref.business_id) == _BUSINESS_ID
    assert str(account_ref.connection_id) == _CONNECTION_ID
    assert (resource, parent_path) == ("containers", "accounts/1")
