"""`RevokePlatformCredential`: `POST /platform-accounts/{id}/revoke`. Pide
al broker que borre el token de su almacen y marca la credencial
`REVOKED` en `credential_refs` -- terminal, solo se sale reconectando
(`BeginOAuthConnect`)."""

from __future__ import annotations

from safent_ads.accounts.application.connect_ports import CredentialRepository, OAuthBrokerPort
from safent_ads.accounts.application.errors import (
    AccountNotFoundError,
    CredentialNotFoundError,
)
from safent_ads.accounts.application.ports import AccountRepository
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class RevokePlatformCredential:
    def __init__(
        self,
        accounts: AccountRepository,
        credentials: CredentialRepository,
        oauth_broker: OAuthBrokerPort,
        clock: Clock,
    ) -> None:
        self._accounts = accounts
        self._credentials = credentials
        self._oauth_broker = oauth_broker
        self._clock = clock

    async def execute(self, *, business_id: BusinessId, account_ref: AccountRef) -> None:
        account = await self._accounts.get_by_ref(account_ref)
        if account is None or account.business_id != business_id:
            raise AccountNotFoundError(str(account_ref))

        credential = await self._credentials.get(account.credential_ref_id, business_id=business_id)
        if credential is None:
            raise CredentialNotFoundError(str(account.credential_ref_id))

        await self._oauth_broker.revoke_credential(account.credential_ref_id)
        credential.revoke(at=self._clock.now())
        await self._credentials.save(credential)
