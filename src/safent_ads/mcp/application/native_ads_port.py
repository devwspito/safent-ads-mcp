"""Business-scoped official MCP reads, independent of storage and transport."""

from typing import Any, Protocol


class BusinessNativeAdsReadPort(Protocol):
    async def list_native_tools(self, business_id: str, account_ref: str) -> dict[str, Any]: ...

    async def read_native_tool(
        self, business_id: str, account_ref: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]: ...
