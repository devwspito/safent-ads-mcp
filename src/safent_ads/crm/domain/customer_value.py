"""`CustomerValue` (VO derivado, data-model.md §CustomerValue, spec 027):
contribucion acumulada (y proyectada) de una identidad. Nunca se almacena
como verdad -- se recalcula en cada lectura sobre `revenue_events`.

Invariante: sin volumen o sin ventana madura, no hay numero, hay un estado
honesto (`no_number_reason`, FR-009 de spec 026, profitability-engine.md
§7 "no hay numero"). Nunca un cero ni una estimacion fabricada."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.shared.ids import BusinessId

MIN_COHORT_SIZE_FOR_A_NUMBER = 5


class CustomerValueInvariantError(ValueError):
    """`CustomerValue` no provisional sin proyeccion, o provisional sin
    motivo -- misma disciplina que `Measure` (shared/read_models/dto.py)."""


@dataclass(frozen=True, kw_only=True, slots=True)
class CustomerValue:
    business_id: BusinessId
    cohort_size: int
    observed_contribution_minor: int
    currency: str
    projected_contribution_minor: int | None
    maturity: float
    horizon_days: int
    is_provisional: bool
    no_number_reason: str | None

    def __post_init__(self) -> None:
        if self.cohort_size < 0:
            raise CustomerValueInvariantError(f"cohort_size negativo: {self.cohort_size}")
        if not (0.0 <= self.maturity <= 1.0):
            raise CustomerValueInvariantError(f"maturity fuera de [0,1]: {self.maturity}")
        if self.horizon_days < 0:
            raise CustomerValueInvariantError(f"horizon_days negativo: {self.horizon_days}")
        if self.is_provisional and not self.no_number_reason:
            raise CustomerValueInvariantError("provisional exige no_number_reason")
        if not self.is_provisional and self.projected_contribution_minor is None:
            raise CustomerValueInvariantError("no provisional exige projected_contribution_minor")

    @classmethod
    def no_number(
        cls,
        *,
        business_id: BusinessId,
        cohort_size: int,
        observed_contribution_minor: int,
        currency: str,
        maturity: float,
        horizon_days: int,
        reason: str,
    ) -> CustomerValue:
        return cls(
            business_id=business_id,
            cohort_size=cohort_size,
            observed_contribution_minor=observed_contribution_minor,
            currency=currency,
            projected_contribution_minor=None,
            maturity=maturity,
            horizon_days=horizon_days,
            is_provisional=True,
            no_number_reason=reason,
        )

    @classmethod
    def observed(
        cls,
        *,
        business_id: BusinessId,
        cohort_size: int,
        observed_contribution_minor: int,
        currency: str,
        projected_contribution_minor: int,
        maturity: float,
        horizon_days: int,
    ) -> CustomerValue:
        return cls(
            business_id=business_id,
            cohort_size=cohort_size,
            observed_contribution_minor=observed_contribution_minor,
            currency=currency,
            projected_contribution_minor=projected_contribution_minor,
            maturity=maturity,
            horizon_days=horizon_days,
            is_provisional=False,
            no_number_reason=None,
        )
