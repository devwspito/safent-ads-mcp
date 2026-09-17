"""Adaptador SQL de `GuardrailSetRepository` sobre `guardrails`
(0007_rules_guardrails, importes en unidades menores desde 0012).

FR-13: el chokepoint recibe el `GuardrailSet` YA compuesto. La composicion
NO se hace en SQL: las filas de los tres ambitos que alcanzan a la entidad
(negocio -> cuenta -> campana) se leen de mas general a mas especifica y se
funden con `GuardrailSet.effective_with`, el mismo servicio de dominio que
usan los tests unitarios. Escribir esa regla dos veces — una en Python y
otra en un `LEAST(...)` de SQL — seria dejar que las dos se separen.

Consecuencia deliberada: un ambito especifico que RELAJA un limite general
no se recorta en silencio, revienta con `GuardrailRelaxationError` (C-17).
Un guardarraíl mas ancho que el de la cuenta que lo contiene es un dato
malo, no una preferencia.

Si ningun ambito tiene guardarrailes, `get_effective` falla: ejecutar sin
limites es exactamente lo que FR-13 prohibe."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.domain.guardrails import (
    GuardrailScope,
    GuardrailSet,
    ScopeKind,
)
from safent_ads.execution.infrastructure.errors import (
    IncompleteGuardrailSetError,
    MissingGuardrailSetError,
    UnknownEntityRefError,
)
from safent_ads.proposals.infrastructure.value_codec import money_from_minor
from safent_ads.shared.ids import EntityLevel, EntityRef, EntityRefFormatError
from safent_ads.shared.physical_ads_sql import PHYSICAL_ENTITY_REFS_SQL

__all__ = ["SqlGuardrailSetRepository"]

_PERCENT: Final = 100.0

_SCOPE_KIND_BY_ROW: Final[dict[str, ScopeKind]] = {
    "business": ScopeKind.BUSINESS,
    "platform_account": ScopeKind.PLATFORM_ACCOUNT,
    "campaign": ScopeKind.ENTITY,
}

# La campana de una entidad es ella misma si es de nivel campana, o su
# ancestro: los guardarrailes de campana alcanzan a sus conjuntos y anuncios.
_ANCESTRY: Final = """
    WITH RECURSIVE ancestry AS (
        SELECT id, parent_id, level, entity_ref, business_id, platform_account_id
          FROM ad_entities WHERE entity_ref = :entity_ref
        UNION ALL
        SELECT parent.id, parent.parent_id, parent.level, parent.entity_ref,
               parent.business_id, parent.platform_account_id
          FROM ad_entities AS parent
          JOIN ancestry ON ancestry.parent_id = parent.id
    )
    SELECT ancestry.business_id,
           ancestry.platform_account_id,
           (SELECT campaign.entity_ref FROM ancestry AS campaign
             WHERE campaign.level = 'campaign') AS campaign_entity_ref
      FROM ancestry
     WHERE ancestry.entity_ref = :entity_ref
"""

_ACCOUNT_BY_REF: Final = """
    SELECT business_id, id AS platform_account_id, NULL AS campaign_entity_ref
      FROM platform_accounts
     WHERE account_ref = :account_ref
"""

# De mas general a mas especifico: ese es el orden en el que se componen.
_EFFECTIVE: Final = f"""
    SELECT scope,
           COALESCE(campaign_entity_ref, platform_account_id::text, business_id::text)
               AS scope_ref,
           currency, daily_cap_minor, monthly_cap_minor, budget_floor_minor,
           budget_ceiling_minor, max_step_pct, max_changes_per_entity_per_day
      FROM guardrails
     WHERE (scope = 'business' AND business_id = :business_id)
        OR (scope = 'platform_account' AND platform_account_id IN (
            SELECT sibling.id FROM platform_accounts sibling JOIN platform_accounts chosen
              ON sibling.business_id = chosen.business_id AND sibling.platform = chosen.platform
             AND sibling.external_account_id = chosen.external_account_id
             WHERE chosen.id = :platform_account_id))
        OR (scope = 'campaign' AND campaign_entity_ref IN (
            {PHYSICAL_ENTITY_REFS_SQL.replace(":entity_ref", ":campaign_entity_ref")}))
     ORDER BY CASE scope
                WHEN 'business' THEN 0 WHEN 'platform_account' THEN 1 ELSE 2
              END
"""  # noqa: S608 - fixed SQL fragment, never caller input


class SqlGuardrailSetRepository:
    """Implementa `execution.application.ports.GuardrailSetRepository`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_effective(self, scope: GuardrailScope) -> GuardrailSet:
        target = await self._resolve(scope)
        result = await self._session.execute(text(_EFFECTIVE), target)
        rows = result.mappings().all()
        if not rows:
            raise MissingGuardrailSetError(
                f"ningun guardarrail alcanza a {scope.ref}: no hay limites que aplicar"
            )
        return replace(_compose(rows), scope=scope)

    async def _resolve(self, scope: GuardrailScope) -> dict[str, Any]:
        """Del ambito que pide el chokepoint a las tres claves que buscan sus
        guardarrailes. `ENTITY` es el caso normal (el chokepoint compone el
        ambito desde la entidad de la propuesta)."""
        if scope.kind is ScopeKind.BUSINESS:
            return {
                "business_id": _business_uuid(scope.ref),
                "platform_account_id": None,
                "campaign_entity_ref": None,
            }
        row = await self._lookup(scope)
        return {
            "business_id": row["business_id"],
            "platform_account_id": row["platform_account_id"],
            "campaign_entity_ref": row["campaign_entity_ref"],
        }

    async def _lookup(self, scope: GuardrailScope) -> RowMapping:
        statement, params = _lookup_query(scope)
        result = await self._session.execute(text(statement), params)
        row = result.mappings().one_or_none()
        if row is None:
            raise UnknownEntityRefError(
                f"{scope.ref} no existe: no se puede saber que guardarrailes le tocan"
            )
        return row


def _lookup_query(scope: GuardrailScope) -> tuple[str, dict[str, Any]]:
    ref = scope.ref
    try:
        entity = EntityRef.parse(ref)
    except EntityRefFormatError:
        return _ACCOUNT_BY_REF, {"account_ref": ref}
    if entity.level == EntityLevel.ACCOUNT:
        return _ACCOUNT_BY_REF, {"account_ref": ref}
    return _ANCESTRY, {"entity_ref": ref}


def _business_uuid(ref: str) -> uuid.UUID:
    try:
        return uuid.UUID(ref)
    except ValueError as exc:
        raise UnknownEntityRefError(
            f"un ambito de negocio necesita el UUID del negocio: {ref!r}"
        ) from exc


def _compose(rows: Sequence[RowMapping]) -> GuardrailSet:
    """Pliega de lo general a lo especifico con el servicio de dominio: cada
    ambito solo puede estrechar al anterior."""
    composed = _to_guardrail_set(rows[0])
    for row in rows[1:]:
        composed = _to_guardrail_set(row).effective_with(composed)
    return composed


def _to_guardrail_set(row: RowMapping) -> GuardrailSet:
    currency = str(row["currency"])
    limits = (
        "daily_cap_minor",
        "monthly_cap_minor",
        "budget_floor_minor",
        "budget_ceiling_minor",
        "max_step_pct",
        "max_changes_per_entity_per_day",
    )
    if any(row[column] is None for column in limits):
        raise IncompleteGuardrailSetError(
            f"el guardarrail de ambito {row['scope']} deja limites sin definir"
        )
    return GuardrailSet(
        scope=GuardrailScope(kind=_SCOPE_KIND_BY_ROW[str(row["scope"])], ref=str(row["scope_ref"])),
        daily_cap=money_from_minor(int(row["daily_cap_minor"]), currency),
        monthly_cap=money_from_minor(int(row["monthly_cap_minor"]), currency),
        floor=money_from_minor(int(row["budget_floor_minor"]), currency),
        ceiling=money_from_minor(int(row["budget_ceiling_minor"]), currency),
        max_step_pct=float(row["max_step_pct"]) / _PERCENT,
        max_changes_per_entity_day=int(row["max_changes_per_entity_per_day"]),
    )
