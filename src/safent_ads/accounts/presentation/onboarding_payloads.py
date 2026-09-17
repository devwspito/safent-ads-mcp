"""DTOs de `GET /api/v1/onboarding` (029 T022, contracts/rest-api.md
§Onboarding). Ningun campo lleva un secreto: solo el `id`/`status` de cada
paso y, cuando esta bloqueado, el motivo -- el mismo criterio de
enmascarado que `platform_apps_payloads.py` aplica a las credenciales que
estos pasos resumen."""

from __future__ import annotations

import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from safent_ads.accounts.application.get_onboarding_status import (
    OnboardingBlockingReason,
    OnboardingStatus,
    OnboardingStep,
    OnboardingStepId,
    OnboardingStepStatus,
)
from safent_ads.accounts.presentation.payloads import StrictModel


class InitialBusinessRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120, strict=True)
    timezone: str = Field(min_length=1, max_length=64, strict=True)
    reference_currency: str = Field(min_length=3, max_length=3, strict=True)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip() or any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Nombre no válido")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Zona horaria no válida") from exc
        return value

    @field_validator("reference_currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        if re.fullmatch(r"[A-Z]{3}", value) is None:
            raise ValueError("Moneda de referencia no válida")
        return value


class OnboardingStepResponse(StrictModel):
    id: OnboardingStepId
    status: OnboardingStepStatus
    blocking_reason: OnboardingBlockingReason | None

    @classmethod
    def from_step(cls, step: OnboardingStep) -> OnboardingStepResponse:
        return cls(id=step.step_id, status=step.status, blocking_reason=step.blocking_reason)


class OnboardingStatusResponse(StrictModel):
    steps: list[OnboardingStepResponse]
    complete: bool

    @classmethod
    def from_status(cls, status: OnboardingStatus) -> OnboardingStatusResponse:
        return cls(
            steps=[OnboardingStepResponse.from_step(step) for step in status.steps],
            complete=status.complete,
        )
