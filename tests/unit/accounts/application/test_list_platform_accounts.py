"""`ListPlatformAccounts`: junta cuenta + salud de la credencial;
`needs_reconnect` por defecto cierra en falso si falta la credencial."""

from __future__ import annotations

import uuid

from safent_ads.accounts.application.list_platform_accounts import ListPlatformAccounts
from safent_ads.accounts.domain.platform_account import ApiTier, PlatformAccount
from safent_ads.accounts.domain.platform_credential import PlatformCredential
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.accounts.testing.in_memory_repositories import (
    InMemoryAccountRepository,
    InMemoryCredentialRepository,
)
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.unit.accounts.application.conftest import NOW

_ACCOUNT_REF = AccountRef(PlatformCode.GOOGLE, "1234567890")


def _account(
    business_id: BusinessId,
    credential_ref_id: CredentialRefId,
    account_ref: AccountRef = _ACCOUNT_REF,
) -> PlatformAccount:
    return PlatformAccount(
        business_id=business_id,
        account_ref=account_ref,
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
        obtained_at=NOW,
    )


async def test_only_lists_accounts_for_the_given_business() -> None:
    business_id = BusinessId.new()
    other_business = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    other_account_ref = AccountRef(PlatformCode.META, "act_999")
    accounts = InMemoryAccountRepository(
        [
            _account(business_id, credential_ref_id),
            _account(other_business, CredentialRefId(uuid.uuid4()), other_account_ref),
        ]
    )
    use_case = ListPlatformAccounts(accounts, InMemoryCredentialRepository())

    views = await use_case.execute(business_id)

    assert len(views) == 1
    assert views[0].account_ref == _ACCOUNT_REF


async def test_connected_credential_does_not_need_reconnect() -> None:
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    credentials = InMemoryCredentialRepository([_credential(business_id, credential_ref_id)])
    use_case = ListPlatformAccounts(accounts, credentials)

    views = await use_case.execute(business_id)

    assert views[0].needs_reconnect is False
    assert views[0].credential_status is not None


async def test_missing_credential_needs_reconnect() -> None:
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    use_case = ListPlatformAccounts(accounts, InMemoryCredentialRepository())

    views = await use_case.execute(business_id)

    assert views[0].needs_reconnect is True
    assert views[0].credential_status is None


async def test_revoked_credential_needs_reconnect() -> None:
    business_id = BusinessId.new()
    credential_ref_id = CredentialRefId(uuid.uuid4())
    credential = _credential(business_id, credential_ref_id)
    credential.revoke(at=NOW)
    accounts = InMemoryAccountRepository([_account(business_id, credential_ref_id)])
    credentials = InMemoryCredentialRepository([credential])
    use_case = ListPlatformAccounts(accounts, credentials)

    views = await use_case.execute(business_id)

    assert views[0].needs_reconnect is True
