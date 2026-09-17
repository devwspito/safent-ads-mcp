"""Adaptador SQL de `rules.application.read_models.guardrails_view` (I-1,
revision final T130): las dos consultas de `GET /guardrails?scope_ref`
viven aqui, detras de `ports.GuardrailViewReadPort`. La proyeccion pura
(`GuardrailView`, `parse_scope_ref`, `row_to_guardrail_view`) sigue en
`application/read_models/guardrails_view.py`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.rules.application.read_models.guardrails_view import (
    GuardrailView,
    row_to_guardrail_view,
)

__all__ = ["SqlGuardrailViewReadPort"]

_SELECT_ACCOUNT_GUARDRAILS: Final = """
    SELECT account.platform AS platform, account.external_account_id AS external_account_id,
           account.account_ref AS account_ref,
           guardrail.currency AS currency,
           guardrail.daily_cap_minor AS daily_cap_minor,
           guardrail.monthly_cap_minor AS monthly_cap_minor,
           guardrail.budget_floor_minor AS budget_floor_minor,
           guardrail.budget_ceiling_minor AS budget_ceiling_minor,
           guardrail.max_step_pct AS max_step_pct,
           guardrail.max_changes_per_entity_per_day AS max_changes_per_entity_per_day
      FROM guardrails AS guardrail
      JOIN platform_accounts AS account ON account.id = guardrail.platform_account_id
     WHERE guardrail.scope = 'platform_account'
       AND guardrail.daily_cap_minor IS NOT NULL
       AND guardrail.monthly_cap_minor IS NOT NULL
       AND guardrail.budget_floor_minor IS NOT NULL
       AND guardrail.budget_ceiling_minor IS NOT NULL
"""
_SELECT_GUARDRAILS_FOR_BUSINESS: Final = text(
    f"{_SELECT_ACCOUNT_GUARDRAILS} AND account.business_id = :business_id "
    "ORDER BY account.platform, account.external_account_id"
)
_SELECT_GUARDRAIL_FOR_ACCOUNT: Final = text(
    f"{_SELECT_ACCOUNT_GUARDRAILS} AND account.account_ref = :account_ref"
)

_SELECT_SETUP_ACCOUNTS: Final = text("""
    SELECT account.account_ref, account.platform, account.external_account_id,
           account.currency, guardrail.currency AS guardrail_currency,
           guardrail.daily_cap_minor, guardrail.monthly_cap_minor,
           guardrail.budget_floor_minor, guardrail.budget_ceiling_minor,
           guardrail.max_step_pct, guardrail.max_changes_per_entity_per_day
      FROM platform_accounts AS account
      LEFT JOIN guardrails AS guardrail
        ON guardrail.platform_account_id = account.id AND guardrail.scope = 'platform_account'
     WHERE account.business_id = :business_id
     ORDER BY account.platform, account.external_account_id, account.account_ref
""")


class SqlGuardrailViewReadPort:
    """Implementa `rules.application.read_models.ports.GuardrailViewReadPort`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_setup_for_business(
        self, *, business_id: str
    ) -> list[tuple[dict[str, Any], GuardrailView | None]]:
        """Include new accounts; missing/incomplete policies are NULL, never defaults."""
        rows = (
            await self._session.execute(_SELECT_SETUP_ACCOUNTS, {"business_id": business_id})
        ).mappings()
        result = []
        required = (
            "daily_cap_minor",
            "monthly_cap_minor",
            "budget_floor_minor",
            "budget_ceiling_minor",
            "max_step_pct",
            "max_changes_per_entity_per_day",
        )
        for row in rows:
            policy = None
            if row["guardrail_currency"] == row["currency"] and all(
                row[field] is not None for field in required
            ):
                policy = row_to_guardrail_view(cast(Mapping[str, Any], row))
            result.append(
                (
                    {
                        "account_ref": row["account_ref"],
                        "platform": row["platform"],
                        "platform_account_id": row["external_account_id"],
                        "display_name": None,  # Account names are not persisted in this schema.
                        "currency": row["currency"],
                    },
                    policy,
                )
            )
        return result

    async def list_for_business(self, *, business_id: str) -> list[GuardrailView]:
        rows = (
            await self._session.execute(
                _SELECT_GUARDRAILS_FOR_BUSINESS, {"business_id": business_id}
            )
        ).mappings()
        return [row_to_guardrail_view(cast(Mapping[str, Any], row)) for row in rows]

    async def list_for_account(
        self, *, platform: str, external_account_id: str
    ) -> list[GuardrailView]:
        row = (
            (
                await self._session.execute(
                    _SELECT_GUARDRAIL_FOR_ACCOUNT,
                    {"account_ref": f"{platform}:{external_account_id}"},
                )
            )
            .mappings()
            .one_or_none()
        )
        return [] if row is None else [row_to_guardrail_view(cast(Mapping[str, Any], row))]
