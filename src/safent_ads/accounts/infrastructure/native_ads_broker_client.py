"""Native read requests use the existing authenticated Unix-socket boundary."""

from pathlib import Path
from typing import Any

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.accounts.infrastructure.broker_client import BrokerSocketClient


class NativeAdsBrokerClient(BrokerSocketClient):
    def __init__(self, socket_path: Path) -> None:
        super().__init__(socket_path, timeout_seconds=45)

    async def list_native_tools(self, account_ref: AccountRef) -> dict[str, Any]:
        # `native_ads_read` es de solo lectura por diseño (`NativeMcpReadGateway`/
        # `native_mcp_policy.py`: catalogo con `"writes_exposed": False` y una
        # lista blanca de herramientas get/search/metadata) -- mismo arranque
        # en frio que el resto de lecturas de `BrokerSocketClient`.
        return dict(
            await self._request(
                {
                    "op": "native_ads_read",
                    "business_id": str(account_ref.business_id)
                    if account_ref.business_id
                    else None,
                    "connection_id": str(account_ref.connection_id)
                    if account_ref.connection_id
                    else None,
                    "platform": account_ref.platform.value,
                    "external_account_id": account_ref.external_account_id,
                    "tool": None,
                    "arguments": {},
                },
                retryable=True,
            )
        )

    async def read_native_tool(
        self, account_ref: AccountRef, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return dict(
            await self._request(
                {
                    "op": "native_ads_read",
                    "business_id": str(account_ref.business_id)
                    if account_ref.business_id
                    else None,
                    "connection_id": str(account_ref.connection_id)
                    if account_ref.connection_id
                    else None,
                    "platform": account_ref.platform.value,
                    "external_account_id": account_ref.external_account_id,
                    "tool": tool,
                    "arguments": arguments,
                },
                retryable=True,
            )
        )
