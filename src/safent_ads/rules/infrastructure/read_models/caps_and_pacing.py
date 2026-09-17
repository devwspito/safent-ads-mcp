"""Adaptador SQL de `rules.application.read_models.caps_and_pacing` (I-1,
revision final T130): la proyeccion pura (topes, ritmo, `to_major`/`money`/
`days_in_month`) vive en `application/read_models/`; aqui solo las dos
consultas que necesitaba -- referencia de la primera cuenta publicitaria de
un negocio (`AccountRefReadPort`) y gasto en un rango."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlAccountRefReadPort", "spend_in_range_minor"]

_SELECT_ONE_ACCOUNT_REF_FOR_BUSINESS = text("""
    SELECT platform, substr(account_ref, length(platform)+2) AS external_account_id
      FROM platform_accounts
     WHERE business_id = :business_id
     ORDER BY created_at
     LIMIT 1
""")

# Extraida de `mcp.infrastructure.sql_portfolio_read_port` (que la llamaba
# dos veces, para "hoy" y para "MTD", con el mismo texto): unica consulta
# de "cuanto ha gastado este negocio en un rango" -- `get_budget_envelope`
# (`mcp.infrastructure.sql_budget_envelope_read_port`) la reutiliza en vez
# de declarar una segunda (instruccion explicita del encargo: "do not write
# a second SQL for spend").
_SELECT_BUSINESS_SPEND_IN_RANGE = text("""
    SELECT COALESCE(SUM(spend), 0) AS spend_minor
      FROM metrics_daily_physical
     WHERE business_id = :business_id AND entity_level = 'campaign'
       AND stat_date BETWEEN :start AND :end
""")


class SqlAccountRefReadPort:
    """Implementa `rules.application.read_models.ports.AccountRefReadPort`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def first_account_ref_for_business(
        self, *, business_id: uuid.UUID | str
    ) -> tuple[str, str] | None:
        row = (
            (
                await self._session.execute(
                    _SELECT_ONE_ACCOUNT_REF_FOR_BUSINESS, {"business_id": business_id}
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else (row["platform"], row["external_account_id"])


async def spend_in_range_minor(
    session: AsyncSession, *, business_id: uuid.UUID | str, start: date, end: date
) -> int:
    """Gasto total (unidades minor) de un negocio en `[start, end]`
    (inclusive) sobre `metrics_daily`. Unica fuente de este numero --
    `sql_portfolio_read_port.get_portfolio_overview` y
    `sql_budget_envelope_read_port.get_budget_envelope` la llaman en vez de
    declarar cada uno su propio `text(...)`."""
    result = await session.execute(
        _SELECT_BUSINESS_SPEND_IN_RANGE, {"business_id": business_id, "start": start, "end": end}
    )
    return int(result.scalar_one())
