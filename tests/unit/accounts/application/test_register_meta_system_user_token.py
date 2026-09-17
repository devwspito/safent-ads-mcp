"""`RegisterMetaSystemUserToken`: via alternativa de Meta -- el broker ya
valido el token contra `me`/`me/adaccounts`; este caso de uso solo
persiste lo que el broker descubrio."""

from __future__ import annotations

import uuid

import pytest

from safent_ads.accounts.application.connect_ports import DiscoveredAccount, OAuthCompleteResult
from safent_ads.accounts.application.errors import (
    BrokerRequestDeniedError,
    OAuthProviderDeniedError,
)
from safent_ads.accounts.application.register_meta_system_user_token import (
    RegisterMetaSystemUserToken,
)
from safent_ads.accounts.domain.platform_account import ApiTier
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.accounts.testing.in_memory_repositories import (
    InMemoryAccountRepository,
    InMemoryCredentialRepository,
)
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.unit.accounts.application.conftest import NOW, FakeOAuthBrokerPort


def _discovered() -> DiscoveredAccount:
    return DiscoveredAccount(
        platform=PlatformCode.META,
        external_account_id="act_555",
        label="Cuenta Meta",
        currency="EUR",
        timezone="Europe/Madrid",
        api_tier=ApiTier.META_LIMITED,
        credential_ref_id=CredentialRefId(uuid.uuid4()),
        scopes=frozenset({"ads_read", "ads_management", "business_management"}),
        obtained_at=NOW,
        expires_at=None,
    )


async def test_creates_account_from_the_pasted_token() -> None:
    business_id = BusinessId.new()
    discovered = _discovered()
    broker = FakeOAuthBrokerPort(complete_result=OAuthCompleteResult(accounts=[discovered]))
    accounts = InMemoryAccountRepository()
    use_case = RegisterMetaSystemUserToken(broker, accounts, InMemoryCredentialRepository())

    created = await use_case.execute(business_id=business_id, token="pasted-token")

    assert len(created) == 1
    stored = await accounts.get_by_ref(AccountRef(PlatformCode.META, "act_555"))
    assert stored is not None
    assert stored.business_id == business_id


async def test_broker_denial_is_translated() -> None:
    broker = FakeOAuthBrokerPort(deny=BrokerRequestDeniedError("OAUTH_PROVIDER_DENIED"))
    use_case = RegisterMetaSystemUserToken(
        broker, InMemoryAccountRepository(), InMemoryCredentialRepository()
    )

    with pytest.raises(OAuthProviderDeniedError):
        await use_case.execute(business_id=BusinessId.new(), token="bad-token")
