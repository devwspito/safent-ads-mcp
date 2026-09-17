"""`GetOnboardingStatus` (029 T022): combina credenciales de VENDOR
(`PlatformAppsBrokerPort`) y cuentas conectadas (`AccountRepository`) sin
tocar ni un socket ni Postgres -- las 4 transiciones de cada paso y la
condicion de `complete`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from safent_ads.accounts.application.get_onboarding_status import (
    GetOnboardingStatus,
    OnboardingBlockingReason,
    OnboardingStepId,
    OnboardingStepStatus,
)
from safent_ads.accounts.application.platform_apps import GetPlatformAppStatus
from safent_ads.accounts.application.platform_apps_ports import PlatformAppStatus
from safent_ads.accounts.domain.platform_account import ApiTier, PlatformAccount
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAccountRepository
from safent_ads.shared.ids import BusinessId, PlatformCode
from tests.unit.accounts.application.conftest import FakePlatformAppsBrokerPort

_NOW = datetime(2026, 9, 10, tzinfo=UTC)
_API_TIER_BY_PLATFORM = {
    PlatformCode.GOOGLE: ApiTier.GOOGLE_STANDARD,
    PlatformCode.META: ApiTier.META_FULL,
}


def _configured_status(platform: PlatformCode, *, configured: bool) -> PlatformAppStatus:
    return PlatformAppStatus(
        platform=platform,
        configured=configured,
        client_id_masked="****abcd" if configured else None,
        login_customer_id_masked=None,
        updated_at=_NOW if configured else None,
    )


def _account(platform: PlatformCode) -> PlatformAccount:
    return PlatformAccount(
        business_id=BusinessId.new(),
        account_ref=AccountRef(platform, "123"),
        currency="EUR",
        timezone="Europe/Madrid",
        api_tier=_API_TIER_BY_PLATFORM[platform],
        credential_ref_id=CredentialRefId(uuid.uuid4()),
    )


def _use_case(
    *, status_result: PlatformAppStatus, accounts: InMemoryAccountRepository
) -> GetOnboardingStatus:
    broker = FakePlatformAppsBrokerPort(status_result=status_result)
    return GetOnboardingStatus(GetPlatformAppStatus(broker), accounts)


async def test_nothing_configured_is_unauthorized_shaped() -> None:
    use_case = _use_case(
        status_result=_configured_status(PlatformCode.GOOGLE, configured=False),
        accounts=InMemoryAccountRepository(),
    )

    result = await use_case.execute()

    by_id = {step.step_id: step for step in result.steps}
    assert by_id[OnboardingStepId.GOOGLE_APP].status == OnboardingStepStatus.PENDING
    assert by_id[OnboardingStepId.GOOGLE_ACCOUNT].status == OnboardingStepStatus.BLOCKED
    assert (
        by_id[OnboardingStepId.GOOGLE_ACCOUNT].blocking_reason
        == OnboardingBlockingReason.GOOGLE_APP_NOT_CONFIGURED
    )
    assert result.complete is False


async def test_app_configured_without_accounts_is_pending_not_blocked() -> None:
    use_case = _use_case(
        status_result=_configured_status(PlatformCode.GOOGLE, configured=True),
        accounts=InMemoryAccountRepository(),
    )

    result = await use_case.execute()

    by_id = {step.step_id: step for step in result.steps}
    assert by_id[OnboardingStepId.GOOGLE_APP].status == OnboardingStepStatus.DONE
    assert by_id[OnboardingStepId.GOOGLE_ACCOUNT].status == OnboardingStepStatus.PENDING
    assert by_id[OnboardingStepId.GOOGLE_ACCOUNT].blocking_reason is None
    assert result.complete is False


async def test_one_connected_account_is_enough_to_be_complete() -> None:
    use_case = _use_case(
        status_result=_configured_status(PlatformCode.GOOGLE, configured=True),
        accounts=InMemoryAccountRepository([_account(PlatformCode.GOOGLE)]),
    )

    result = await use_case.execute()

    by_id = {step.step_id: step for step in result.steps}
    assert by_id[OnboardingStepId.GOOGLE_ACCOUNT].status == OnboardingStepStatus.DONE
    assert by_id[OnboardingStepId.META_ACCOUNT].status != OnboardingStepStatus.DONE
    assert result.complete is True


async def test_response_never_carries_masked_or_raw_credential_fields() -> None:
    use_case = _use_case(
        status_result=_configured_status(PlatformCode.GOOGLE, configured=True),
        accounts=InMemoryAccountRepository(),
    )

    result = await use_case.execute()

    for step in result.steps:
        assert not hasattr(step, "client_id_masked")
        assert not hasattr(step, "developer_token_masked")


@pytest.mark.parametrize("platform", [PlatformCode.GOOGLE, PlatformCode.META])
async def test_broker_status_is_queried_for_both_platforms(platform: PlatformCode) -> None:
    broker = FakePlatformAppsBrokerPort(
        status_result=_configured_status(platform, configured=False)
    )
    use_case = GetOnboardingStatus(GetPlatformAppStatus(broker), InMemoryAccountRepository())

    result = await use_case.execute()

    assert {step.step_id for step in result.steps} == {
        OnboardingStepId.GOOGLE_APP,
        OnboardingStepId.GOOGLE_ACCOUNT,
        OnboardingStepId.META_APP,
        OnboardingStepId.META_ACCOUNT,
    }
