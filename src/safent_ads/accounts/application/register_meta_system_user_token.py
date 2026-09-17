"""`RegisterMetaSystemUserToken`: via alternativa de Meta,
`POST /platform-accounts/meta/system-user-token`. Sin `state`/PKCE porque
no hay redireccion -- el propietario pega el token directamente; el
broker lo valida contra Meta (`me` + `me/adaccounts`) antes de que este
caso de uso vea ningun resultado, asi que un token invalido nunca llega a
crear una cuenta (contracts/rest-api.md §Conexiones)."""

from __future__ import annotations

from collections.abc import Sequence

from safent_ads.accounts.application._upsert_discovered_account import upsert_discovered_account
from safent_ads.accounts.application.connect_ports import CredentialRepository, OAuthBrokerPort
from safent_ads.accounts.application.errors import (
    BrokerRequestDeniedError,
    OAuthProviderDeniedError,
)
from safent_ads.accounts.application.ports import AccountRepository
from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.shared.ids import BusinessId


class RegisterMetaSystemUserToken:
    def __init__(
        self,
        oauth_broker: OAuthBrokerPort,
        accounts: AccountRepository,
        credentials: CredentialRepository,
    ) -> None:
        self._oauth_broker = oauth_broker
        self._accounts = accounts
        self._credentials = credentials

    async def execute(
        self,
        *,
        business_id: BusinessId,
        token: str,
        owner_id: str | None = None,
    ) -> Sequence[PlatformAccount]:
        try:
            result = await self._oauth_broker.register_meta_system_user_token(
                token,
                business_id=str(business_id),
                owner_id=owner_id,
            )
        except BrokerRequestDeniedError as exc:
            raise OAuthProviderDeniedError(exc.error_code) from exc

        return [
            await upsert_discovered_account(
                self._accounts, self._credentials, business_id, discovered
            )
            for discovered in result.accounts
        ]
