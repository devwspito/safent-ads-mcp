"""Read-only native MCP boundary. Writes remain on AdsPlatformPort.execute_write."""

from typing import Any, Protocol

from safent_ads.accounts.domain.refs import AccountRef


class NativeAdsReadPort(Protocol):
    async def list_native_tools(self, account_ref: AccountRef) -> dict[str, Any]: ...

    async def read_native_tool(
        self, account_ref: AccountRef, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]: ...
