"""Check business ownership before any native MCP account access."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.application.native_ads import NativeAdsReadPort
from safent_ads.mcp.infrastructure.account_ownership import resolve_owned_account_ref


class BrokerNativeAdsReadPort:
    def __init__(
        self, port: NativeAdsReadPort, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        self._port = port
        self._sessions = sessions

    async def list_native_tools(self, business_id: str, account_ref: str) -> dict[str, Any]:
        account = await resolve_owned_account_ref(self._sessions, business_id, account_ref)
        return await self._port.list_native_tools(account)

    async def read_native_tool(
        self, business_id: str, account_ref: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        account = await resolve_owned_account_ref(self._sessions, business_id, account_ref)
        return await self._port.read_native_tool(account, tool, arguments)
