"""Adaptador SQL de `BrakeStatePort` sobre `emergency_brakes`
(0007_rules_guardrails).

threat-model.md C-18: "freno = fila en BD leida en CADA ruta de ejecucion,
nunca cacheada". Aqui eso es literal: `get` consulta siempre, no guarda nada
entre llamadas y no tiene estado que invalidar. Un freno que el propietario
enciende con el trabajador a mitad de ciclo se ve en la siguiente lectura,
que es lo que hace util un interruptor de emergencia (FR-14).

El indice unico protege cada fila de cuenta. El lock fisico compartido
serializa las mutaciones entre conexiones hermanas; un segundo engage se
rechaza, no rebaja ni reemplaza silenciosamente el freno ya activo.

Traducciones de frontera:
- `BrakeScope.ref` de `execution` es opaco. Para `PLATFORM_ACCOUNT` puede
  llegar como `EntityRef` (`meta:campaign:123`, que es lo que compone
  `brake_scope_from` desde el ambito de la entidad) o como referencia de
  cuenta (`meta:act_1`). Las dos se resuelven al `platform_account_id` real;
  un `ref` que no existe no se ignora, se denuncia.
- `mode` va en mayusculas en el esquema y en minusculas en el dominio.
- Un freno liberado sigue siendo una fila: `get` la devuelve con
  `engaged=False`, igual que el doble en memoria, para que `release` sepa
  que ese ambito tuvo freno alguna vez."""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.application.toggle_emergency_brake import BrakeAlreadyEngagedError
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    EmergencyBrake,
    most_restrictive_brake,
)
from safent_ads.execution.infrastructure.errors import UnknownEntityRefError
from safent_ads.shared.ids import EntityLevel, EntityRef, EntityRefFormatError
from safent_ads.shared.physical_ads_sql import BRAKE_SCOPE_MATCH_SQL, PHYSICAL_ACCOUNT_LOCK_SQL

__all__ = ["DEFAULT_BRAKE_ACTOR", "SqlBrakeStatePort"]

# `emergency_brakes.engaged_by`/`released_by` son NOT NULL y el dominio de
# `execution` no lleva autor: `EmergencyBrake` modela el interruptor, no
# quien lo pulso. Hasta que el panel (otra rama) pase el propietario
# autenticado, la fila dice de donde vino la pulsacion en vez de inventar un
# nombre de persona.
DEFAULT_BRAKE_ACTOR: Final = "execution.toggle_emergency_brake"

_SCOPE_MATCH: Final = BRAKE_SCOPE_MATCH_SQL

# Gana el freno activo; si no hay ninguno, el ultimo que se libero.
_GET_TEMPLATE: Final = """
    SELECT mode, reason, engaged_at, released_at
      FROM emergency_brakes
     WHERE {scope_match}
     ORDER BY (released_at IS NULL) DESC,
              (released_at IS NULL AND mode = 'ALL') DESC, engaged_at DESC, id DESC
     LIMIT 1
"""
_GET: Final = _GET_TEMPLATE.format(scope_match=_SCOPE_MATCH)

_ENGAGE: Final = """
    INSERT INTO emergency_brakes (scope_kind, business_id, platform_account_id, mode, reason,
                                  engaged_by, engaged_at)
    VALUES (:scope_kind, :business_id, :platform_account_id, :mode, :reason, :actor, :at)
    ON CONFLICT (scope_kind, scope_key) WHERE released_at IS NULL
    DO UPDATE SET mode = CASE WHEN emergency_brakes.mode = 'ALL' THEN 'ALL'
                             ELSE EXCLUDED.mode END, reason = EXCLUDED.reason,
                  engaged_by = EXCLUDED.engaged_by, engaged_at = EXCLUDED.engaged_at
"""

_RELEASE_TEMPLATE: Final = """
    UPDATE emergency_brakes
       SET released_at = :at, released_by = :actor
     WHERE {scope_match} AND released_at IS NULL
"""
_RELEASE: Final = _RELEASE_TEMPLATE.format(scope_match=_SCOPE_MATCH)

_ACCOUNT_BY_ENTITY: Final = """
    SELECT platform_account_id, business_id FROM ad_entities WHERE entity_ref = :ref
"""
_ACCOUNT_BY_REF: Final = """
    SELECT id AS platform_account_id, business_id FROM platform_accounts
     WHERE account_ref = :account_ref
"""


class SqlBrakeStatePort:
    """Implementa `execution.application.ports.BrakeStatePort`. Sin cache,
    sin memoria entre llamadas: es el punto del sistema donde eso importa."""

    def __init__(self, session: AsyncSession, *, actor: str = DEFAULT_BRAKE_ACTOR) -> None:
        self._session = session
        self._actor = actor

    async def get(self, scope: BrakeScope) -> EmergencyBrake | None:
        result = await self._session.execute(text(_GET), await self._scope_params(scope))
        row = result.mappings().one_or_none()
        return None if row is None else _to_brake(scope, row)

    async def get_effective(self, scope: BrakeScope) -> EmergencyBrake | None:
        candidates = await self._effective_candidates(scope)
        brakes = [await self.get(candidate) for candidate in candidates]
        return most_restrictive_brake(brakes)

    async def _effective_candidates(self, scope: BrakeScope) -> list[BrakeScope]:
        """De mas ancho a mas estrecho, el mismo orden que
        `guardrails.most_restrictive_brake` usa para desempatar entre
        frenos activos con el mismo modo."""
        global_scope = BrakeScope(kind=BrakeScopeKind.GLOBAL)
        if scope.kind is BrakeScopeKind.GLOBAL:
            return [scope]
        if scope.kind is BrakeScopeKind.BUSINESS:
            return [global_scope, scope]
        business_id = await self._resolve_business(scope)
        return [global_scope, BrakeScope(kind=BrakeScopeKind.BUSINESS, ref=str(business_id)), scope]

    async def save(self, brake: EmergencyBrake) -> None:
        params = await self._scope_params(brake.scope)
        if params["platform_account_id"] is not None:
            await self._session.execute(text(PHYSICAL_ACCOUNT_LOCK_SQL), params)
        if not brake.engaged:
            await self._session.execute(
                text(_RELEASE), {**params, "at": brake.since, "actor": self._actor}
            )
            return
        # Recheck AFTER the physical lock: get/save may race across processes.
        # A successful response/audit must never claim a weaker mode than the
        # persisted brake. Require explicit release before engaging again.
        existing = (await self._session.execute(text(_GET), params)).mappings().one_or_none()
        if existing is not None and existing["released_at"] is None:
            raise BrakeAlreadyEngagedError("physical account already has an active brake")
        await self._session.execute(
            text(_ENGAGE),
            {
                **params,
                "mode": brake.mode.value.upper(),
                "reason": brake.reason,
                "actor": self._actor,
                "at": brake.since,
            },
        )

    async def _scope_params(self, scope: BrakeScope) -> dict[str, Any]:
        if scope.kind is BrakeScopeKind.GLOBAL:
            return {"scope_kind": "global", "business_id": None, "platform_account_id": None}
        if scope.kind is BrakeScopeKind.BUSINESS:
            return {
                "scope_kind": "business",
                "business_id": _business_uuid(scope),
                "platform_account_id": None,
            }
        return {
            "scope_kind": "platform_account",
            "business_id": None,
            "platform_account_id": await self._resolve_account(scope),
        }

    async def _resolve_account(self, scope: BrakeScope) -> uuid.UUID:
        account_id, _business_id = await self._resolve_account_and_business(scope)
        return account_id

    async def _resolve_business(self, scope: BrakeScope) -> uuid.UUID:
        _account_id, business_id = await self._resolve_account_and_business(scope)
        return business_id

    async def _resolve_account_and_business(self, scope: BrakeScope) -> tuple[uuid.UUID, uuid.UUID]:
        """El `ref` llega como entidad (`brake_scope_from` lo compone desde
        el ambito de la propuesta) o como cuenta. Cualquier otra cosa es un
        ambito que no existe: denegar antes que mirar el freno equivocado."""
        ref = scope.ref or ""
        try:
            entity = EntityRef.parse(ref)
        except EntityRefFormatError:
            row = await self._account_row_by_account_ref(ref)
        else:
            if entity.level == EntityLevel.ACCOUNT:
                row = await self._account_row_by_account_ref(ref)
            else:
                result = await self._session.execute(text(_ACCOUNT_BY_ENTITY), {"ref": ref})
                row = result.mappings().one_or_none()
        if row is None:
            raise UnknownEntityRefError(f"el freno apunta a un ambito inexistente: {ref!r}")
        return uuid.UUID(str(row["platform_account_id"])), uuid.UUID(str(row["business_id"]))

    async def _account_row_by_account_ref(self, ref: str) -> RowMapping | None:
        platform, _, external_account_id = ref.partition(":")
        if not external_account_id:
            return None
        result = await self._session.execute(
            text(_ACCOUNT_BY_REF),
            {"account_ref": ref},
        )
        return result.mappings().one_or_none()


def _business_uuid(scope: BrakeScope) -> uuid.UUID:
    try:
        return uuid.UUID(scope.ref or "")
    except ValueError as exc:
        raise UnknownEntityRefError(
            f"un freno de negocio necesita el UUID del negocio: {scope.ref!r}"
        ) from exc


def _to_brake(scope: BrakeScope, row: RowMapping) -> EmergencyBrake:
    released_at = row["released_at"]
    engaged = released_at is None
    return EmergencyBrake(
        scope=scope,
        mode=BrakeMode(str(row["mode"]).lower()),
        engaged=engaged,
        reason=str(row["reason"]) if engaged else None,
        since=row["engaged_at"] if engaged else released_at,
    )
