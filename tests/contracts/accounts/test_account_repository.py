"""Contrato de `AccountRepository`: identico para el doble en memoria y para
`SqlAccountRepository`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.accounts.domain.platform_account import PlatformAccountStatus
from safent_ads.shared.errors import DomainError
from safent_ads.shared.ids import PlatformCode
from tests.contracts.accounts.conftest import (
    RepositoryFixture,
    account_ref,
    build_account,
    new_business_id,
)


async def test_saved_account_is_read_back_whole(repositories: RepositoryFixture) -> None:
    business_id = new_business_id()
    await repositories.given_business(business_id, PlatformCode.META)
    synced_at = datetime(2026, 9, 9, 10, 30, tzinfo=UTC)
    account = build_account(business_id, last_synced_at=synced_at)

    await repositories.accounts.save(account)
    stored = await repositories.accounts.get_by_ref(account.account_ref)

    assert stored is not None
    assert stored.business_id == business_id
    assert stored.account_ref == account.account_ref
    assert stored.currency == "EUR"
    assert stored.timezone == "Europe/Madrid"
    assert stored.api_tier == account.api_tier
    assert stored.credential_ref_id == account.credential_ref_id
    assert stored.status == PlatformAccountStatus.ACTIVE
    assert stored.last_synced_at == synced_at


async def test_unknown_account_is_none(repositories: RepositoryFixture) -> None:
    assert await repositories.accounts.get_by_ref(account_ref(suffix="ausente")) is None


async def test_remote_account_cannot_be_reassigned_to_another_business(
    repositories: RepositoryFixture,
) -> None:
    original = new_business_id()
    intruder = new_business_id()
    await repositories.given_business(original, PlatformCode.META)
    await repositories.given_business(intruder, PlatformCode.META)
    account = build_account(original)
    await repositories.accounts.save(account)

    with pytest.raises(DomainError):
        await repositories.accounts.save(build_account(intruder, ref=account.account_ref))

    stored = await repositories.accounts.get_by_ref(account.account_ref)
    assert stored is not None and stored.business_id == original
    assert await repositories.accounts.list_by_business(intruder) == []


async def test_save_twice_updates_instead_of_duplicating(
    repositories: RepositoryFixture,
) -> None:
    business_id = new_business_id()
    await repositories.given_business(business_id, PlatformCode.META)
    account = build_account(business_id)
    await repositories.accounts.save(account)

    account.mark_throttled(occurred_at=datetime(2026, 9, 9, 11, tzinfo=UTC))
    await repositories.accounts.save(account)

    stored = await repositories.accounts.list_by_business(business_id)
    assert len(stored) == 1
    assert stored[0].status == PlatformAccountStatus.THROTTLED


async def test_list_by_business_only_returns_its_own(repositories: RepositoryFixture) -> None:
    mine = new_business_id()
    other = new_business_id()
    await repositories.given_business(mine, PlatformCode.META)
    await repositories.given_business(other, PlatformCode.META)
    await repositories.accounts.save(build_account(mine, ref=account_ref(PlatformCode.META, "mia")))
    await repositories.accounts.save(
        build_account(mine, ref=account_ref(PlatformCode.GOOGLE, "mia-google"))
    )
    await repositories.accounts.save(
        build_account(other, ref=account_ref(PlatformCode.META, "ajena"))
    )

    stored = await repositories.accounts.list_by_business(mine)

    assert {account.account_ref.external_account_id for account in stored} == {
        "act_mia",
        "act_mia-google",
    }


async def test_read_only_status_round_trips(repositories: RepositoryFixture) -> None:
    business_id = new_business_id()
    await repositories.given_business(business_id, PlatformCode.META)
    account = build_account(business_id)
    account.mark_read_only()

    await repositories.accounts.save(account)
    stored = await repositories.accounts.get_by_ref(account.account_ref)

    assert stored is not None
    assert stored.status == PlatformAccountStatus.READ_ONLY
