"""Topes y ritmo de cartera: proyeccion de lectura compartida por `panel` y
`mcp` (N7, plan.md §4: ninguno de los dos importa al otro). Vivia duplicada
byte a byte en `panel.infrastructure.sql_read_model` y
`mcp.infrastructure.sql_portfolio_read_port` antes de esta extraccion.

Depende del puerto `rules.application.ports.GuardrailRepository` (N4) y de
`signals.domain.pacing` (N3) -- por eso vive en `rules` (N4, el mas alto de
los dos) y no en `shared` (N0): moverla a `shared` invertiria la regla de
dependencias de plan.md §4 (N0 no puede depender de N3/N4). La
implementacion concreta (`SqlGuardrailRepository`) la construyen los
llamadores en `infrastructure`, nunca este modulo.

La referencia de cuenta (`AccountRefReadPort`, I-1 revision final T130) es
tambien un puerto: la consulta SQL que lo implementa
(`SqlAccountRefReadPort`) vive en `rules.infrastructure.read_models
.caps_and_pacing`, igual que `spend_in_range_minor` -- este modulo se queda
solo con la proyeccion pura, sin `sqlalchemy`.

Limitacion heredada de ambos contextos: los topes/ritmo de cartera se leen
de la PRIMERA cuenta publicitaria del negocio -- combinar guardarrailes de
varias cuentas en un unico tope de cartera no esta definido en ningun
contrato todavia."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from safent_ads.rules.application.ports import GuardrailRepository
from safent_ads.shared.read_models.dto import Caps, CapsSource, Money, Pacing
from safent_ads.signals.domain.errors import ZeroBaselineError
from safent_ads.signals.domain.pacing import pace_index, projected_spend

if TYPE_CHECKING:
    from safent_ads.rules.application.read_models.ports import AccountRefReadPort

_MINOR_UNITS_PER_MAJOR = 100


def to_major(minor: int | None) -> Decimal:
    if minor is None:
        return Decimal(0)
    return Decimal(minor) / _MINOR_UNITS_PER_MAJOR


def money(minor: int | None, currency: str) -> Money:
    return Money(to_major(minor), currency)


def days_in_month(today: date) -> int:
    next_month = today.replace(day=28) + timedelta(days=4)
    return (next_month.replace(day=1) - timedelta(days=1)).day


async def caps_and_pacing(
    account_refs: AccountRefReadPort,
    guardrails: GuardrailRepository,
    *,
    business_id: uuid.UUID | str,
    spend_mtd_minor: int,
    today: date,
    month_start: date,
) -> tuple[Caps, Pacing, bool]:
    """Devuelve `(caps, pacing, is_partial)`. `is_partial=True` cuando no
    hay cuenta publicitaria o no hay guardarrail sembrado todavia -- la
    lectura honesta es "sin topes conocidos", nunca fallar la vista."""
    account_ref_row = await account_refs.first_account_ref_for_business(business_id=business_id)
    if account_ref_row is None:
        return (
            Caps(daily=None, monthly=None, source=CapsSource.GUARDRAIL),
            Pacing(index_pct=0.0, projection_pct=0.0, days_remaining=0),
            True,
        )
    platform, external_account_id = account_ref_row
    account_ref = f"{platform}:{external_account_id}"
    policy = await guardrails.find_for_account(account_ref=account_ref)
    if policy is None:
        return (
            Caps(daily=None, monthly=None, source=CapsSource.GUARDRAIL),
            Pacing(index_pct=0.0, projection_pct=0.0, days_remaining=0),
            True,
        )
    days_total = days_in_month(today)
    days_elapsed = (today - month_start).days + 1
    try:
        index = pace_index(
            actual_mtd_minor=spend_mtd_minor,
            monthly_cap_minor=policy.monthly_cap_minor,
            days_elapsed=days_elapsed,
            days_in_month=days_total,
        )
        projection_minor = projected_spend(
            actual_mtd_minor=spend_mtd_minor,
            days_elapsed=days_elapsed,
            days_in_month=days_total,
        )
        projection_pct = (
            projection_minor / policy.monthly_cap_minor * 100 if policy.monthly_cap_minor else 0.0
        )
    except ZeroBaselineError:
        index, projection_pct = 0.0, 0.0
    caps = Caps(
        daily=money(policy.daily_cap_minor, "EUR"),
        monthly=money(policy.monthly_cap_minor, "EUR"),
        source=CapsSource.GUARDRAIL,
    )
    pacing = Pacing(
        index_pct=round(index * 100, 1),
        projection_pct=round(projection_pct, 1),
        days_remaining=days_total - days_elapsed,
    )
    return caps, pacing, False
