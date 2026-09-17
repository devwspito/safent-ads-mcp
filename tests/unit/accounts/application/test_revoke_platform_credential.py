"""`RevokePlatformCredential`: revoca en el broker y marca la credencial
`REVOKED`, con aislamiento por negocio (IDOR)."""

from __future__ import annotations

import uuid

import pytest

from safent_ads.accounts.application.errors import AccountNotFoundError, CredentialNotFoundError
from safent_ads.accounts.application.revoke_platform_credential import RevokePlatformCredential
from safent_ads.accounts.domain.platform_account import ApiTier, PlatformAccount
from safent_ads.accounts.domain.platform_credential import CredentialStatus, PlatformCredential
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.accounts.testing.in_memory_repositories import (
    InMemoryAccountRepository,
    InMemoryCredentialRepository,
)
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.unit.accounts.application.conftest import NOW, FakeOAuthBrokerPort

_ACCOUNT_REF = AccountRef(PlatformCode.GOOGLE, "1234567890")


def _account(business_id: BusinessId, credential_ref_id: CredentialRefId) -> PlatformAccount:
    return PlatformAccount(
        business_id=business_id,
        account_ref=_ACCOUNT_REF,
        currency="EUR",
        timezone="Europe/Madrid",
        api_tier=ApiTier.GOOGLE_EXPLORER,
        credential_ref_id=credential_ref_id,
    )


def _credential(business_id: BusinessId, credential_ref_id: CredentialRefId) -> PlatformCredential:
    return PlatformCredential(
        credential_ref_id=credential_ref_id,
        business_id=business_id,
        platform=PlatformCode.GOOGLE,
        alias=str(credential_ref_id),
        scopes=frozenset({"adwords"}),
    )


async def test_revokes_credential_and_calls_the_broker() -> None:
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    credentials = InMemoryCredentialRepository([_credential(business_id, credential_ref_id)])
    broker = FakeOAuthBrokerPort()
    use_case = RevokePlatformCredential(accounts, credentials, broker, FixedClock(NOW))

    await use_case.execute(business_id=business_id, account_ref=_ACCOUNT_REF)

    stored = await credentials.get(credential_ref_id, business_id=business_id)
    assert stored is not None
    assert stored.status == CredentialStatus.REVOKED
    assert broker.revoked == [credential_ref_id]


async def test_unknown_account_raises() -> None:
    use_case = RevokePlatformCredential(
        InMemoryAccountRepository(),
        InMemoryCredentialRepository(),
        FakeOAuthBrokerPort(),
        FixedClock(NOW),
    )

    with pytest.raises(AccountNotFoundError):
        await use_case.execute(business_id=BusinessId.new(), account_ref=_ACCOUNT_REF)


async def test_account_from_another_business_is_not_found() -> None:
    owner_business = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(owner_business, credential_ref_id)])
    use_case = RevokePlatformCredential(
        accounts, InMemoryCredentialRepository(), FakeOAuthBrokerPort(), FixedClock(NOW)
    )

    with pytest.raises(AccountNotFoundError):
        await use_case.execute(business_id=BusinessId.new(), account_ref=_ACCOUNT_REF)


async def test_missing_credential_raises() -> None:
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    use_case = RevokePlatformCredential(
        accounts, InMemoryCredentialRepository(), FakeOAuthBrokerPort(), FixedClock(NOW)
    )

    with pytest.raises(CredentialNotFoundError):
        await use_case.execute(business_id=business_id, account_ref=_ACCOUNT_REF)
