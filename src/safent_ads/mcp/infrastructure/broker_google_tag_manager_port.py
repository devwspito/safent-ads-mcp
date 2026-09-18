"""Broker-backed GTM reader with business ownership checked before I/O."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.infrastructure.broker_client import BrokerSocketClient
from safent_ads.mcp.infrastructure.account_ownership import resolve_owned_account_ref
from safent_ads.mcp.infrastructure.broker_call import call_broker


class GoogleTagManagerBrokerClient(BrokerSocketClient):
    async def read(
        self,
        *,
        business_id: str,
        connection_id: str,
        external_account_id: str,
        resource: str,
        parent_path: str | None,
    ) -> Mapping[str, Any]:
        result = await self._request(
            {
                "op": "google_tag_manager_read",
                "platform": "google",
                "business_id": business_id,
                "connection_id": connection_id,
                "external_account_id": external_account_id,
                "resource": resource,
                "parent_path": parent_path,
            },
            retryable=True,
        )
        return cast(Mapping[str, Any], result)


class BrokerGoogleTagManagerReadPort:
    def __init__(
        self,
        socket_path: Path,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self._client = GoogleTagManagerBrokerClient(socket_path)
        self._sessions = sessions

    async def read(
        self,
        business_id: str,
        account_ref: str,
        *,
        resource: str,
        parent_path: str | None,
    ) -> Mapping[str, Any]:
        account = await resolve_owned_account_ref(self._sessions, business_id, account_ref)
        if account.connection_id is None:
            raise ValueError("Google Tag Manager requiere una conexion OAuth con alcance")
        return await call_broker(
            self._client.read(
                business_id=business_id,
                connection_id=str(account.connection_id),
                external_account_id=account.external_account_id,
                resource=resource,
                parent_path=parent_path,
            )
        )
