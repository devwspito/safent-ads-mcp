"""`ComputeCustomerValue` (spec 027, contracts/crm-link.md §3 `GET
/economics/customer-value`): valor de cliente = contribucion acumulada
(devoluciones incluidas con su signo). Sin volumen suficiente, ningun
numero -- un estado honesto (FR-009 de spec 026)."""

from __future__ import annotations

from safent_ads.crm.application.customer_ports import RevenueEventRepository
from safent_ads.crm.domain.customer_value import (
    MIN_COHORT_SIZE_FOR_A_NUMBER,
    CustomerValue,
)
from safent_ads.shared.ids import BusinessId

_DEFAULT_CURRENCY = "EUR"
_DEFAULT_HORIZON_DAYS = 90
# Maturity conservador cuando la cohorte ya alcanza el volumen minimo:
# sin curva de madurez propia todavia (esa vive en `economics.lag_curve`,
# fuera de alcance de T014-T017), se declara maduro solo cuando el volumen
# lo respalda -- Assumption documentada, ver informe de la tarea.
_MATURE_ENOUGH = 1.0


class ComputeCustomerValue:
    def __init__(self, *, revenue_events: RevenueEventRepository) -> None:
        self._revenue_events = revenue_events

    async def execute(
        self, *, business_id: BusinessId, entity_ref: str | None = None
    ) -> CustomerValue:
        stats = await self._revenue_events.cohort_stats(
            business_id=business_id, entity_ref=entity_ref
        )
        currency = stats.currency or _DEFAULT_CURRENCY

        if stats.cohort_size < MIN_COHORT_SIZE_FOR_A_NUMBER:
            return CustomerValue.no_number(
                business_id=business_id,
                cohort_size=stats.cohort_size,
                observed_contribution_minor=stats.observed_contribution_minor,
                currency=currency,
                maturity=0.0,
                horizon_days=_DEFAULT_HORIZON_DAYS,
                reason=f"cohorte de {stats.cohort_size} clientes: sin numero",
            )

        return CustomerValue.observed(
            business_id=business_id,
            cohort_size=stats.cohort_size,
            observed_contribution_minor=stats.observed_contribution_minor,
            currency=currency,
            projected_contribution_minor=stats.observed_contribution_minor,
            maturity=_MATURE_ENOUGH,
            horizon_days=_DEFAULT_HORIZON_DAYS,
        )
