"""Application boundary for account-scoped Google Tag Manager reads."""

from collections.abc import Mapping
from typing import Any, Protocol


class GoogleTagManagerReadPort(Protocol):
    async def read(
        self,
        business_id: str,
        account_ref: str,
        *,
        resource: str,
        parent_path: str | None,
    ) -> Mapping[str, Any]: ...
