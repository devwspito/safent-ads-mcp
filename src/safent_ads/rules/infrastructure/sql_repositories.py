"""Repositorios SQL de `rules` sobre `rules`, `guardrails`,
`emergency_brakes` y `rule_firings` (migraciones 0007 y 0012).

`sync_catalog` es la costura entre `rules.yaml` y la base: primero llama a
`seed_rule_catalog()`, la funcion que dejo la migracion 0007, para que los
codigos nazcan con la postura acordada (NOTIFY y deshabilitados, D-A1), y
luego vuelca condiciones, ventana, accion, magnitud y cooldown desde el
fichero. **Nunca** escribe `autonomy_level` ni `is_enabled`: eso lo decide
el propietario y vive solo en la base (tasks.md §Notas 4).

El resto del vocabulario del dominio va en mayusculas en el esquema y en
minusculas en el codigo; la traduccion esta aqui y en ningun otro sitio.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind
from safent_ads.rules.domain.emergency_brake import BrakeMode, BrakeScope, EmergencyBrake
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.rule_firing import RuleFiring
from safent_ads.rules.domain.stored_rule import StoredRule
from safent_ads.rules.infrastructure.errors import (
    DuplicateRuleCodeError,
    IncompleteGuardrailRowError,
    UnknownRuleCodeError,
)
from safent_ads.shared.ids import EntityLevel, EntityRef, PlatformCode
from safent_ads.shared.physical_ads_sql import BRAKE_SCOPE_MATCH_SQL, PHYSICAL_ACCOUNT_LOCK_SQL

__all__ = [
    "SqlEmergencyBrakeRepository",
    "SqlGuardrailRepository",
    "SqlRuleFiringRepository",
    "SqlRuleRepository",
]

# Ninguna regla del catalogo declara cuantas veces puede dispararse al dia;
# el esquema lo exige para poder habilitarla (data-model.md §Rule). Una vez
# por dia y entidad es el limite conservador, y el propietario lo sube desde
# el panel.
_DEFAULT_MAX_FIRINGS_PER_DAY: Final = 1
_MINUTES_PER_HOUR: Final = 60

_UPSERT_RULE: Final = """
    INSERT INTO rules (code, scope, platform, entity_level, description, source_url,
                       condition, thresholds, data_window, action, magnitude_pct,
                       cooldown_minutes, max_firings_per_day, calibrated_at)
    VALUES (:code, 'global', :platform, :entity_level, :description, :source_url,
            CAST(:condition AS jsonb), CAST(:thresholds AS jsonb), :data_window,
            :action, :magnitude_pct,
            :cooldown_minutes, :max_firings_per_day, now())
    ON CONFLICT ON CONSTRAINT rules_code_scope_unique DO UPDATE
        SET platform            = EXCLUDED.platform,
            entity_level        = EXCLUDED.entity_level,
            description         = EXCLUDED.description,
            source_url          = EXCLUDED.source_url,
            condition           = EXCLUDED.condition,
            thresholds          = EXCLUDED.thresholds,
            data_window         = EXCLUDED.data_window,
            action              = EXCLUDED.action,
            magnitude_pct       = EXCLUDED.magnitude_pct,
            cooldown_minutes    = EXCLUDED.cooldown_minutes,
            max_firings_per_day = COALESCE(rules.max_firings_per_day,
                                           EXCLUDED.max_firings_per_day),
            calibrated_at       = now()
"""

_SELECT_RULE: Final = """
    SELECT code, platform, entity_level, description, source_url, condition, action,
           magnitude_pct, autonomy_level, cooldown_minutes, is_enabled
      FROM rules
     WHERE scope = 'global'
"""

_LIST_ENABLED: Final = f"{_SELECT_RULE} AND is_enabled ORDER BY code"
_LIST_ENABLED_BY_PLATFORM: Final = (
    f"{_SELECT_RULE} AND is_enabled AND platform IS NOT DISTINCT FROM :platform ORDER BY code"
)
_FIND_RULE: Final = f"{_SELECT_RULE} AND code = :code"

# `condition <> '{}'::jsonb`: mismo criterio que `get_by_code` -- una fila
# sembrada por `seed_rule_catalog()` (migracion 0007) pero aun sin calibrar
# no es un catalogo real, es un hueco a medio llenar (GET /rules, panel).
_LIST_ALL: Final = f"""
    {_SELECT_RULE}
      AND condition <> '{{}}'::jsonb
      AND (CAST(:platform AS TEXT) IS NULL OR platform IS NOT DISTINCT FROM :platform)
      AND (CAST(:enabled AS BOOLEAN) IS NULL OR is_enabled = :enabled)
    ORDER BY code
"""

_INSERT_NEW_RULE: Final = """
    INSERT INTO rules (code, scope, platform, entity_level, description, source_url,
                       condition, thresholds, data_window, action, magnitude_pct,
                       cooldown_minutes, max_firings_per_day, autonomy_level, is_enabled,
                       calibrated_at)
    VALUES (:code, 'global', :platform, :entity_level, :description, :source_url,
            CAST(:condition AS jsonb), CAST(:thresholds AS jsonb), :data_window,
            :action, :magnitude_pct, :cooldown_minutes, :max_firings_per_day,
            :autonomy_level, :enabled, now())
    ON CONFLICT ON CONSTRAINT rules_code_scope_unique DO NOTHING
    RETURNING id
"""

# Subconsulta de cuenta, escrita entera en cada sentencia: componer SQL con
# f-strings dispara ruff S608 y, sobre todo, esconde la forma real de la
# consulta a quien la lee.
_UPSERT_GUARDRAIL: Final = """
    INSERT INTO guardrails (scope, platform_account_id, currency, daily_cap_minor,
                            monthly_cap_minor, budget_floor_minor, budget_ceiling_minor,
                            max_step_pct, max_changes_per_entity_per_day)
    SELECT 'platform_account', account.id, :currency, :daily_cap, :monthly_cap, :floor,
           :ceiling, :max_step_pct, :max_changes
      FROM platform_accounts AS account
     WHERE account.account_ref = :account_ref
    ON CONFLICT ON CONSTRAINT guardrails_scope_unique DO UPDATE
        SET currency                       = EXCLUDED.currency,
            daily_cap_minor                = EXCLUDED.daily_cap_minor,
            monthly_cap_minor              = EXCLUDED.monthly_cap_minor,
            budget_floor_minor             = EXCLUDED.budget_floor_minor,
            budget_ceiling_minor           = EXCLUDED.budget_ceiling_minor,
            max_step_pct                   = EXCLUDED.max_step_pct,
            max_changes_per_entity_per_day = EXCLUDED.max_changes_per_entity_per_day
    RETURNING id
"""

_FIND_GUARDRAIL: Final = """
    SELECT guardrail.daily_cap_minor, guardrail.monthly_cap_minor,
           guardrail.budget_floor_minor, guardrail.budget_ceiling_minor,
           guardrail.max_step_pct, guardrail.max_changes_per_entity_per_day
      FROM guardrails AS guardrail
      JOIN platform_accounts AS account ON account.id = guardrail.platform_account_id
     WHERE guardrail.scope = 'platform_account'
       AND account.account_ref = :account_ref
"""

_FIND_ACCOUNT_ID: Final = """
    SELECT id FROM platform_accounts
     WHERE account_ref = :account_ref
"""


class SqlRuleRepository:
    """`RuleRepository` (rules/application/ports.py)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def sync_catalog(self, rules: Sequence[Rule]) -> int:
        await self._session.execute(text("SELECT seed_rule_catalog()"))
        for rule in rules:
            await self._session.execute(text(_UPSERT_RULE), _rule_params(rule))
        await self._session.flush()
        return len(rules)

    async def get_by_code(self, code: str) -> StoredRule | None:
        """`None` tanto si el codigo no existe como si la fila la sembro
        `seed_rule_catalog()` (migracion 0007) pero `sync_catalog` todavia
        no la ha calibrado con las clausulas de `rules.yaml`: `condition`
        vale `{}` (`DEFAULT '{}'::jsonb`) y no tiene `clauses` que leer. El
        puerto de condicion (`execution/infrastructure/sql_rule_condition.py`)
        trata `None` como "no aplica" y deniega por defecto -- una fila a
        medias no debe tumbar la lectura con un `KeyError`."""
        result = await self._session.execute(text(_FIND_RULE), {"code": code})
        row = result.mappings().one_or_none()
        if row is None or not row["condition"]:
            return None
        return _to_stored_rule(row)

    async def list_enabled(self, *, platform: PlatformCode | None = None) -> Sequence[StoredRule]:
        if platform is None:
            result = await self._session.execute(text(_LIST_ENABLED))
        else:
            result = await self._session.execute(
                text(_LIST_ENABLED_BY_PLATFORM), {"platform": platform.value}
            )
        return [_to_stored_rule(row) for row in result.mappings()]

    async def set_autonomy(self, *, code: str, level: AutonomyLevel, enabled: bool) -> None:
        result = await self._session.execute(
            text(
                """
                UPDATE rules SET autonomy_level = :level, is_enabled = :enabled
                 WHERE code = :code AND scope = 'global'
                RETURNING id
                """
            ),
            {"code": code, "level": level.value.upper(), "enabled": enabled},
        )
        if result.first() is None:
            raise UnknownRuleCodeError(f"{code} no esta en el catalogo de la base")
        await self._session.flush()

    async def list_all(
        self, *, platform: PlatformCode | None = None, enabled: bool | None = None
    ) -> Sequence[StoredRule]:
        result = await self._session.execute(
            text(_LIST_ALL),
            {"platform": None if platform is None else platform.value, "enabled": enabled},
        )
        return [_to_stored_rule(row) for row in result.mappings()]

    async def create(self, rule: Rule, *, enabled: bool) -> StoredRule:
        params = {
            **_rule_params(rule),
            "autonomy_level": rule.autonomy_level.value.upper(),
            "enabled": enabled,
        }
        result = await self._session.execute(text(_INSERT_NEW_RULE), params)
        if result.mappings().one_or_none() is None:
            raise DuplicateRuleCodeError(f"{rule.code} ya existe en el catalogo")
        await self._session.flush()
        return StoredRule(rule=rule, is_enabled=enabled)


class SqlGuardrailRepository:
    """`GuardrailRepository` en el ambito de cuenta de plataforma."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_for_account(
        self, *, account_ref: str, policy: GuardrailPolicy, currency: str
    ) -> None:
        params = _account_params(account_ref)
        result = await self._session.execute(
            text(_UPSERT_GUARDRAIL),
            {
                **params,
                "currency": currency,
                "daily_cap": policy.daily_cap_minor,
                "monthly_cap": policy.monthly_cap_minor,
                "floor": policy.floor_minor,
                "ceiling": policy.ceiling_minor,
                "max_step_pct": policy.max_step_pct,
                "max_changes": policy.max_changes_per_day,
            },
        )
        if result.first() is None:
            raise IncompleteGuardrailRowError(f"cuenta desconocida: {account_ref}")
        await self._session.flush()

    async def find_for_account(self, *, account_ref: str) -> GuardrailPolicy | None:
        result = await self._session.execute(text(_FIND_GUARDRAIL), _account_params(account_ref))
        row = result.mappings().one_or_none()
        if row is None:
            return None
        if any(row[column] is None for column in row):
            raise IncompleteGuardrailRowError(
                f"guardarrail incompleto para {account_ref}: no se puede componer la politica"
            )
        return GuardrailPolicy(
            daily_cap_minor=int(row["daily_cap_minor"]),
            monthly_cap_minor=int(row["monthly_cap_minor"]),
            floor_minor=int(row["budget_floor_minor"]),
            ceiling_minor=int(row["budget_ceiling_minor"]),
            max_step_pct=float(row["max_step_pct"]),
            max_changes_per_day=int(row["max_changes_per_entity_per_day"]),
        )


class SqlEmergencyBrakeRepository:
    """Physical account brakes, serialized across every OAuth connection.

    Historical duplicates are read most-restrictive-first and an explicitly
    authorized release closes the whole physical scope, never another business.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def engage(self, brake: EmergencyBrake) -> None:
        params = await self._scope_params(brake.scope)
        if params["platform_account_id"] is not None:
            await self._session.execute(text(PHYSICAL_ACCOUNT_LOCK_SQL), params)
            if await self.find_active(scope=brake.scope) is not None:
                raise ValueError("physical account already has an active brake")
        await self._session.execute(
            text(
                """
                INSERT INTO emergency_brakes (scope_kind, business_id, platform_account_id,
                                              mode, reason, engaged_by, engaged_at)
                VALUES (:scope_kind, :business_id, :platform_account_id, :mode, :reason,
                        :engaged_by, :engaged_at)
                """
            ),
            {
                **params,
                "mode": brake.mode.value.upper(),
                "reason": brake.reason,
                "engaged_by": brake.engaged_by,
                "engaged_at": brake.engaged_at,
            },
        )
        await self._session.flush()

    async def release(self, *, scope: BrakeScope, released_by: str, released_at: datetime) -> None:
        params = await self._scope_params(scope)
        if params["platform_account_id"] is not None:
            await self._session.execute(text(PHYSICAL_ACCOUNT_LOCK_SQL), params)
        await self._session.execute(
            text(
                f"""
                UPDATE emergency_brakes
                   SET released_at = :released_at, released_by = :released_by
                 WHERE {BRAKE_SCOPE_MATCH_SQL}
                   AND released_at IS NULL
                """  # noqa: S608 - fixed scope SQL; values remain bound parameters
            ),
            {
                **params,
                "released_at": released_at,
                "released_by": released_by,
            },
        )
        await self._session.flush()

    async def find_active(self, *, scope: BrakeScope) -> EmergencyBrake | None:
        result = await self._session.execute(
            text(
                f"""
                SELECT mode, reason, engaged_by, engaged_at, released_at
                  FROM emergency_brakes
                 WHERE {BRAKE_SCOPE_MATCH_SQL}
                   AND released_at IS NULL
                 ORDER BY (mode = 'ALL') DESC, engaged_at DESC, id DESC LIMIT 1
                """  # noqa: S608 - fixed scope SQL; values remain bound parameters
            ),
            await self._scope_params(scope),
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None
        return EmergencyBrake(
            scope=scope,
            mode=BrakeMode(row["mode"].lower()),
            reason=row["reason"],
            engaged_by=row["engaged_by"],
            engaged_at=row["engaged_at"],
            released_at=row["released_at"],
        )

    async def _scope_params(self, scope: BrakeScope) -> Mapping[str, Any]:
        account_id: UUID | None = None
        if scope.platform_account_ref is not None:
            result = await self._session.execute(
                text(_FIND_ACCOUNT_ID), _account_params(scope.platform_account_ref)
            )
            account_id = result.scalar_one()
        return {
            "scope_kind": scope.kind.value,
            "business_id": None if scope.business_id is None else scope.business_id.value,
            "platform_account_id": account_id,
        }


class SqlRuleFiringRepository:
    """`RuleFiringRepository`. `cycle_id` no viaja en el dominio: por defecto
    cada anotacion lleva el suyo, y quien orquesta un ciclo puede fijarlo
    para que reejecutarlo no vuelva a contar el mismo disparo (UNIQUE
    `(rule_id, entity_ref, cycle_id)` del esquema)."""

    def __init__(
        self, session: AsyncSession, *, cycle_id_factory: Callable[[], UUID] = uuid4
    ) -> None:
        self._session = session
        self._cycle_id_factory = cycle_id_factory

    async def record(self, firing: RuleFiring) -> None:
        result = await self._session.execute(
            text(
                """
                INSERT INTO rule_firings (rule_id, business_id, entity_ref, outcome, cycle_id,
                                          fired_at)
                SELECT rule.id, entity.business_id, :entity_ref, :outcome, :cycle_id, :fired_at
                  FROM rules AS rule
                  JOIN ad_entities AS entity ON entity.entity_ref = :entity_ref
                 WHERE rule.code = :rule_code AND rule.scope = 'global'
                ON CONFLICT ON CONSTRAINT rule_firings_cycle_unique DO UPDATE
                    SET outcome = EXCLUDED.outcome, fired_at = EXCLUDED.fired_at
                RETURNING id
                """
            ),
            {
                "rule_code": firing.rule_code,
                "entity_ref": str(firing.entity_ref),
                "outcome": firing.outcome.value.upper(),
                "cycle_id": self._cycle_id_factory(),
                "fired_at": firing.fired_at,
            },
        )
        if result.first() is None:
            raise UnknownRuleCodeError(
                f"{firing.rule_code} o {firing.entity_ref} no existen: no se anota el disparo"
            )
        await self._session.flush()

    async def last_fired_at(self, *, rule_code: str, entity_ref: EntityRef) -> datetime | None:
        result = await self._session.execute(
            text(
                """
                SELECT max(firing.fired_at)
                  FROM rule_firings AS firing
                  JOIN rules AS rule ON rule.id = firing.rule_id
                 WHERE rule.code = :rule_code AND firing.entity_ref = :entity_ref
                """
            ),
            {"rule_code": rule_code, "entity_ref": str(entity_ref)},
        )
        return result.scalar_one_or_none()

    async def count_on_day(self, *, rule_code: str, entity_ref: EntityRef, day: date) -> int:
        result = await self._session.execute(
            text(
                """
                SELECT count(*)
                  FROM rule_firings AS firing
                  JOIN rules AS rule ON rule.id = firing.rule_id
                 WHERE rule.code = :rule_code AND firing.entity_ref = :entity_ref
                   AND (firing.fired_at AT TIME ZONE 'UTC')::date = :day
                """
            ),
            {"rule_code": rule_code, "entity_ref": str(entity_ref), "day": day},
        )
        return int(result.scalar_one())


def _account_params(account_ref: str) -> dict[str, Any]:
    return {"account_ref": account_ref}


def _rule_params(rule: Rule) -> Mapping[str, Any]:
    clauses = rule.condition.clauses
    return {
        "code": rule.code,
        "platform": None if rule.platform is None else rule.platform.value,
        "entity_level": rule.entity_level.value,
        "description": rule.description,
        "source_url": rule.source_url,
        "condition": json.dumps(
            {
                "clauses": [
                    {
                        "metric": clause.metric,
                        "comparator": clause.comparator.value,
                        "window": clause.window,
                        "threshold_kind": clause.threshold_kind.value,
                        "value": clause.value,
                    }
                    for clause in clauses
                ]
            }
        ),
        # Copia plana para que el panel edite umbrales sin entender la forma
        # completa de la condicion.
        "thresholds": json.dumps({clause.metric: clause.value for clause in clauses}),
        "data_window": clauses[0].window,
        "action": rule.action_kind.value.upper(),
        "magnitude_pct": rule.magnitude_pct,
        "cooldown_minutes": int(rule.cooldown.total_seconds() // _MINUTES_PER_HOUR),
        "max_firings_per_day": _DEFAULT_MAX_FIRINGS_PER_DAY,
    }


def _to_stored_rule(row: RowMapping) -> StoredRule:
    condition = Condition(
        clauses=tuple(
            ConditionClause(
                metric=clause["metric"],
                comparator=Comparator(clause["comparator"]),
                window=clause["window"],
                threshold_kind=ThresholdKind(clause["threshold_kind"]),
                value=clause["value"],
            )
            for clause in row["condition"]["clauses"]
        )
    )
    return StoredRule(
        rule=Rule(
            code=row["code"],
            platform=None if row["platform"] is None else PlatformCode(row["platform"]),
            entity_level=EntityLevel(row["entity_level"]),
            description=row["description"],
            condition=condition,
            action_kind=ActionKind(row["action"].lower()),
            magnitude_pct=None if row["magnitude_pct"] is None else float(row["magnitude_pct"]),
            autonomy_level=AutonomyLevel(row["autonomy_level"].lower()),
            cooldown=timedelta(minutes=int(row["cooldown_minutes"])),
            source_url=row["source_url"],
        ),
        is_enabled=row["is_enabled"],
    )
