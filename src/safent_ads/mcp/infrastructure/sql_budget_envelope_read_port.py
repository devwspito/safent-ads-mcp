"""`BudgetEnvelopeReadPort` real (P2, tool-surface.md §2.1): techo mensual
y margen restante por NEGOCIO (no por entidad), sobre las mismas piezas que
`rules.application.read_models.caps_and_pacing` -- reutiliza su consulta de
gasto (`spend_in_range_minor`, unica fuente de "cuanto se ha gastado este
mes"; nunca una segunda consulta SQL propia) y su lectura de guardarrail
(`SqlGuardrailRepository.find_for_account`). No delega en la funcion
`caps_and_pacing` completa: esa produce `Money`/porcentajes pensados para
`PortfolioOverview`, y este puerto necesita unidades minor y la moneda real
de la cuenta para el contrato `{monthly_cap_minor, spent_month_to_date_minor,
headroom_minor, projected_month_end_minor, currency, as_of}`.

Honesto ante la ausencia de datos (nunca un numero inventado): sin cuenta
publicitaria conectada, todo queda a `None`
(`reason="no_platform_account"`); con cuenta pero sin guardarrail sembrado,
el techo y el margen quedan a `None` (`reason="no_caps_entry"`) -- pero el
gasto real del mes y su proyeccion SI se reportan, porque son hechos
medidos/proyectados a partir de `metrics_daily`, no derivados de un tope
que no existe. Misma limitacion heredada que `caps_and_pacing`: se lee de
la PRIMERA cuenta publicitaria del negocio."""

from __future__ import annotations

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.infrastructure.sql_repositories import SqlAccountRepository
from safent_ads.mcp.application.search_terms_ports import BudgetEnvelope
from safent_ads.rules.application.read_models.caps_and_pacing import days_in_month
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.rules.infrastructure.read_models.caps_and_pacing import spend_in_range_minor
from safent_ads.rules.infrastructure.sql_repositories import SqlGuardrailRepository
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId
from safent_ads.signals.domain.pacing import projected_spend

__all__ = ["SqlBudgetEnvelopeReadPort"]

_REASON_NO_ACCOUNT = "no_platform_account"
_REASON_NO_CAPS_ENTRY = "no_caps_entry"


class SqlBudgetEnvelopeReadPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def get_budget_envelope(self, business_id: str) -> BudgetEnvelope:
        today = self._clock.now().date()
        month_start = today.replace(day=1)
        async with self._session_factory() as session:
            accounts = await SqlAccountRepository(session).list_by_business(
                BusinessId.parse(business_id)
            )
            if not accounts:
                return _envelope_without_account(business_id, today)
            account_ref = str(accounts[0].account_ref)
            currency = accounts[0].currency
            spent_minor = await spend_in_range_minor(
                session, business_id=business_id, start=month_start, end=today
            )
            policy = await SqlGuardrailRepository(session).find_for_account(
                account_ref=account_ref
            )
        projected_minor = _project_month_end(spent_minor, today, month_start)
        return _envelope_with_account(
            business_id=business_id,
            today=today,
            currency=currency,
            spent_minor=spent_minor,
            projected_minor=projected_minor,
            policy=policy,
        )


def _envelope_without_account(business_id: str, today: date) -> BudgetEnvelope:
    return BudgetEnvelope(
        business_id=business_id,
        monthly_cap_minor=None,
        spent_month_to_date_minor=None,
        headroom_minor=None,
        projected_month_end_minor=None,
        currency=None,
        as_of=today,
        reason=_REASON_NO_ACCOUNT,
    )


def _envelope_with_account(
    *,
    business_id: str,
    today: date,
    currency: str,
    spent_minor: int,
    projected_minor: int,
    policy: GuardrailPolicy | None,
) -> BudgetEnvelope:
    if policy is None:
        return BudgetEnvelope(
            business_id=business_id,
            monthly_cap_minor=None,
            spent_month_to_date_minor=spent_minor,
            headroom_minor=None,
            projected_month_end_minor=projected_minor,
            currency=currency,
            as_of=today,
            reason=_REASON_NO_CAPS_ENTRY,
        )
    return BudgetEnvelope(
        business_id=business_id,
        monthly_cap_minor=policy.monthly_cap_minor,
        spent_month_to_date_minor=spent_minor,
        headroom_minor=policy.monthly_cap_minor - spent_minor,
        projected_month_end_minor=projected_minor,
        currency=currency,
        as_of=today,
        reason=None,
    )


def _project_month_end(spent_minor: int, today: date, month_start: date) -> int:
    days_elapsed = (today - month_start).days + 1
    return round(
        projected_spend(
            actual_mtd_minor=spent_minor,
            days_elapsed=days_elapsed,
            days_in_month=days_in_month(today),
        )
    )
