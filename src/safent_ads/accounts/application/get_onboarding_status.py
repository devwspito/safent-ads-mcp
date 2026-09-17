"""`GetOnboardingStatus`: `GET /api/v1/onboarding` (029 T022, US2). Combina
las credenciales de VENDOR ya configuradas (`PlatformAppsBrokerPort.get_app_status`,
la misma lectura que `GET /platform-apps`) con si hay al menos una cuenta
conectada por plataforma (`AccountRepository.exists_for_platform`, sin
`business_id`: el estado de onboarding es por instalacion, igual que las
credenciales de VENDOR) para que el puente `/ads/` de Safent distinga
`unauthorized` (ninguna app de vendor configurada) de `no_accounts` (app
configurada, cero cuentas conectadas) sin reimplementar ninguna de las dos
lecturas."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from safent_ads.accounts.application.platform_apps import GetPlatformAppStatus
from safent_ads.accounts.application.ports import AccountRepository
from safent_ads.shared.ids import PlatformCode

__all__ = [
    "GetOnboardingStatus",
    "OnboardingStatus",
    "OnboardingStep",
    "OnboardingStepId",
    "OnboardingStepStatus",
]


class OnboardingStepId(StrEnum):
    GOOGLE_APP = "google_app"
    GOOGLE_ACCOUNT = "google_account"
    META_APP = "meta_app"
    META_ACCOUNT = "meta_account"


class OnboardingStepStatus(StrEnum):
    DONE = "done"
    PENDING = "pending"
    BLOCKED = "blocked"


class OnboardingBlockingReason(StrEnum):
    """Motivo por el que un paso de cuenta aun no es alcanzable -- solo se
    usa junto a `OnboardingStepStatus.BLOCKED`, nunca con `PENDING`/`DONE`."""

    GOOGLE_APP_NOT_CONFIGURED = "google_app_not_configured"
    META_APP_NOT_CONFIGURED = "meta_app_not_configured"


@dataclass(frozen=True, slots=True, kw_only=True)
class OnboardingStep:
    step_id: OnboardingStepId
    status: OnboardingStepStatus
    blocking_reason: OnboardingBlockingReason | None


@dataclass(frozen=True, slots=True, kw_only=True)
class OnboardingStatus:
    steps: tuple[OnboardingStep, ...]
    complete: bool


class GetOnboardingStatus:
    def __init__(
        self, app_status: GetPlatformAppStatus, accounts: AccountRepository
    ) -> None:
        self._app_status = app_status
        self._accounts = accounts

    async def execute(self) -> OnboardingStatus:
        google_app_done = (await self._app_status.execute(PlatformCode.GOOGLE)).configured
        meta_app_done = (await self._app_status.execute(PlatformCode.META)).configured
        google_account_done = await self._accounts.exists_for_platform(PlatformCode.GOOGLE)
        meta_account_done = await self._accounts.exists_for_platform(PlatformCode.META)

        steps = (
            _step(OnboardingStepId.GOOGLE_APP, done=google_app_done),
            _account_step(
                OnboardingStepId.GOOGLE_ACCOUNT,
                done=google_account_done,
                app_done=google_app_done,
                blocking_reason=OnboardingBlockingReason.GOOGLE_APP_NOT_CONFIGURED,
            ),
            _step(OnboardingStepId.META_APP, done=meta_app_done),
            _account_step(
                OnboardingStepId.META_ACCOUNT,
                done=meta_account_done,
                app_done=meta_app_done,
                blocking_reason=OnboardingBlockingReason.META_APP_NOT_CONFIGURED,
            ),
        )
        return OnboardingStatus(steps=steps, complete=google_account_done or meta_account_done)


def _step(step_id: OnboardingStepId, *, done: bool) -> OnboardingStep:
    status = OnboardingStepStatus.DONE if done else OnboardingStepStatus.PENDING
    return OnboardingStep(step_id=step_id, status=status, blocking_reason=None)


def _account_step(
    step_id: OnboardingStepId,
    *,
    done: bool,
    app_done: bool,
    blocking_reason: OnboardingBlockingReason,
) -> OnboardingStep:
    if done:
        return OnboardingStep(
            step_id=step_id, status=OnboardingStepStatus.DONE, blocking_reason=None
        )
    if not app_done:
        return OnboardingStep(
            step_id=step_id, status=OnboardingStepStatus.BLOCKED, blocking_reason=blocking_reason
        )
    return OnboardingStep(
        step_id=step_id, status=OnboardingStepStatus.PENDING, blocking_reason=None
    )
