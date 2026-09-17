"""`ListPlatformAccounts`: `GET /platform-accounts`
(contracts/rest-api.md §Conexiones). Junta `PlatformAccount` con la salud
de su `PlatformCredential` -- `needs_reconnect` es el campo que el panel
usa para pintar el boton "Reconectar" sin que el navegador vea nunca el
propio token."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from safent_ads.accounts.application.connect_ports import CredentialRepository
from safent_ads.accounts.application.ports import AccountRepository
from safent_ads.accounts.domain.platform_account import (
    ApiTier,
    PlatformAccount,
    PlatformAccountStatus,
)
from safent_ads.accounts.domain.platform_credential import CredentialStatus, PlatformCredential
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, slots=True, kw_only=True)
class PlatformAccountConnectionView:
    account_ref: AccountRef
    label: str
    status: PlatformAccountStatus
    currency: str
    timezone: str
    api_tier: ApiTier
    credential_status: CredentialStatus | None
    credential_expires_at: datetime | None
    credential_last_validated_at: datetime | None
    credential_checked_at: datetime | None
    credential_last_error_code: str | None
    needs_reconnect: bool
    last_synced_at: datetime | None


class ListPlatformAccounts:
    def __init__(self, accounts: AccountRepository, credentials: CredentialRepository) -> None:
        self._accounts = accounts
        self._credentials = credentials

    async def execute(self, business_id: BusinessId) -> Sequence[PlatformAccountConnectionView]:
        accounts = await self._accounts.list_by_business(business_id)
        views = []
        for account in accounts:
            credential = await self._credentials.get(
                account.credential_ref_id, business_id=business_id
            )
            views.append(_to_view(account, credential))
        return views


def _to_view(
    account: PlatformAccount, credential: PlatformCredential | None
) -> PlatformAccountConnectionView:
    return PlatformAccountConnectionView(
        account_ref=account.account_ref,
        # `platform_accounts.label` llega con otra migracion (data-model.md
        # §PlatformAccount: "por defecto el externo"); hasta entonces, la
        # referencia legible es el propio AccountRef.
        label=str(account.account_ref),
        status=account.status,
        currency=account.currency,
        timezone=account.timezone,
        api_tier=account.api_tier,
        credential_status=credential.status if credential else None,
        credential_expires_at=credential.expires_at if credential else None,
        credential_last_validated_at=credential.last_validated_at if credential else None,
        credential_checked_at=credential.checked_at if credential else None,
        credential_last_error_code=credential.last_error_code if credential else None,
        needs_reconnect=credential.needs_reconnect() if credential else True,
        last_synced_at=account.last_synced_at,
    )
