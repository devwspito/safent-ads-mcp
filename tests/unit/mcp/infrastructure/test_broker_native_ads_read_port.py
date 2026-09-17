"""Ownership is checked before discovery as well as report execution."""

from unittest.mock import AsyncMock

import pytest

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.mcp.infrastructure import broker_native_ads_read_port as module
from safent_ads.shared.ids import PlatformCode


@pytest.mark.parametrize("discovery", [False, True])
async def test_foreign_account_never_reaches_native_broker(monkeypatch, discovery):
    ownership = AsyncMock(side_effect=ValueError("foreign account"))
    monkeypatch.setattr(module, "resolve_owned_account_ref", ownership)
    broker = AsyncMock()
    port = module.BrokerNativeAdsReadPort(broker, object())
    with pytest.raises(ValueError, match="foreign account"):
        if discovery:
            await port.list_native_tools("business", "google:123")
        else:
            await port.read_native_tool("business", "google:123", "search_search", {})
    broker.list_native_tools.assert_not_called()
    broker.read_native_tool.assert_not_called()


async def test_owned_account_is_resolved_server_side(monkeypatch):
    account = AccountRef(PlatformCode.GOOGLE, "1234567890")
    monkeypatch.setattr(module, "resolve_owned_account_ref", AsyncMock(return_value=account))
    broker = AsyncMock()
    port = module.BrokerNativeAdsReadPort(broker, object())
    await port.read_native_tool("business", "google:1234567890", "search_search", {"limit": 10})
    broker.read_native_tool.assert_awaited_once_with(account, "search_search", {"limit": 10})
