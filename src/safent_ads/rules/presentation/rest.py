"""Borde HTTP de `rules`/guardarrailes (contracts/rest-api.md §Reglas y
guardarraíles): funciones puras que `composition/execution_rest.py` llama
para no repetir mapeo/validacion en cada handler -- vive aqui, no en
`composition/`, porque no necesita `Container` (plan.md §4, mismo criterio
que separa dominio de orquestacion).

`PUT /rules/{id}` en esta rama solo edita `autonomy_level`/`enabled`
(`RuleRepository.set_autonomy`, lo unico que el propietario puede tocar sin
desplegar -- `rules/infrastructure/sql_repositories.py`): el resto del
cuerpo del contrato (`condition`/`window`/`action`/`magnitude_pct`/
`cooldown`) exigiria un metodo de escritura completo en `RuleRepository`
que no existe todavia (fuera del alcance de esta rama, escalado a
tech-lead). `apply_autonomy_level` sigue revalidando el invariante de
`Rule.__post_init__` (FR-11/FR-12) contra la regla ya calibrada antes de
persistir nada.

`build_rules_router` (rama gap-accounts-rules) SI necesita `Container`
-- `GET`/`POST /rules`, `DELETE /rules/{id}`, `POST
/rules/{id}/simulate` y `GET /guardrails` abren su propia sesion por
peticion, igual que `composition/execution_rest.py::build_execution_
router`. Vive aqui y no alli porque el encargo de esta rama fija que las
rutas nuevas de `rules`/`guardrails` no deben seguir engordando ese
fichero -- `rules.presentation` es el bounded context dueño de estas
rutas."""

from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.domain.ad_entity import AdEntity
from safent_ads.accounts.infrastructure.sql_repositories import SqlAdEntityRepository
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.composition.container import Container
from safent_ads.iam.infrastructure.sql_business_directory import SqlBusinessDirectory
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import (
    AuthenticatedCaller,
    CallerDep,
    ensure_business_access,
    require_business_access,
)
from safent_ads.rules.application.create_rule import CreateRule, CreateRuleCommand
from safent_ads.rules.application.read_models.guardrails_view import (
    GuardrailView,
    InvalidScopeRefError,
    parse_scope_ref,
)
from safent_ads.rules.application.read_models.rule_catalog_view import (
    RuleView,
    list_rule_views,
    stored_rule_to_view,
)
from safent_ads.rules.domain.autonomy import ActionKind, AutonomyLevel
from safent_ads.rules.domain.condition import Comparator, Condition, ConditionClause, ThresholdKind
from safent_ads.rules.domain.errors import (
    BlankRuleCodeError,
    EmptyConditionError,
    InvalidCooldownError,
    SpendIncreasingAutoRuleError,
)
from safent_ads.rules.domain.rule import Rule
from safent_ads.rules.domain.rule_evaluator import (
    RuleOutcome,
    evaluate_creative_rule,
    evaluate_rule,
)
from safent_ads.rules.domain.stored_rule import StoredRule
from safent_ads.rules.infrastructure.errors import DuplicateRuleCodeError
from safent_ads.rules.infrastructure.read_models.guardrails_view import SqlGuardrailViewReadPort
from safent_ads.rules.infrastructure.read_models.rule_catalog_view import SqlRuleActivityReadPort
from safent_ads.rules.infrastructure.sql_repositories import SqlRuleRepository
from safent_ads.shared.ids import (
    BusinessId,
    EntityLevel,
    EntityRef,
    EntityRefFormatError,
    PlatformCode,
)
from safent_ads.signals.infrastructure.sql_repositories import (
    SqlCreativeSignalRepository,
    SqlSignalRepository,
)

__all__ = [
    "GATE_KEYS",
    "GateKeyInfo",
    "apply_autonomy_level",
    "build_rules_router",
    "stored_rule_to_json",
]


@dataclasses.dataclass(frozen=True, slots=True)
class GateKeyInfo:
    key: str
    label: str
    recommended_default: str


# spec.md preguntas abiertas 2/3/8: texto e identificadores fijados por
# contracts/rest-api.md (`q2_autonomous_decrease`/`q3_monthly_cap`/
# `q8_browser_path`); la redaccion exacta de cada pregunta es Assumption
# documentada, pendiente de que el propietario la confirme -- el valor
# recomendado de `q8` seguro (nunca navegador automatizado) coincide con
# `composition/app.py::_build_website_brand_discovery(enable_playwright_pass=False)`.
GATE_KEYS: tuple[GateKeyInfo, ...] = (
    GateKeyInfo(
        key="q2_autonomous_decrease",
        label="Permitir que el motor de reglas baje presupuesto sin aprobacion",
        recommended_default="true",
    ),
    GateKeyInfo(
        key="q3_monthly_cap",  # gitleaks:allow (clave de confirmacion, no un secreto)
        label="Tope mensual de gasto que el motor puede mover de forma autonoma",
        recommended_default="sin_tope",
    ),
    GateKeyInfo(
        key="q8_browser_path",
        label="Permitir acciones via navegador automatizado cuando la API no cubre la palanca",
        recommended_default="false",
    ),
)

_GATE_KEY_NAMES = frozenset(info.key for info in GATE_KEYS)


def is_known_gate_key(key: str) -> bool:
    return key in _GATE_KEY_NAMES


def apply_autonomy_level(rule: Rule, level: AutonomyLevel) -> Rule:
    """Reconstruye `Rule` con el nuevo `autonomy_level`: `dataclasses.replace`
    vuelve a ejecutar `__post_init__`, asi que el invariante "AUTO nunca sube
    gasto" (FR-11/FR-12) se revalida en el borde HTTP, antes de tocar la
    base -- 422 `AUTO_WOULD_INCREASE_SPEND`, nunca una fila inconsistente."""
    try:
        return dataclasses.replace(rule, autonomy_level=level)
    except SpendIncreasingAutoRuleError as exc:
        raise ApiError(status_code=422, code="AUTO_WOULD_INCREASE_SPEND", message=str(exc)) from exc


def stored_rule_to_json(stored: StoredRule) -> dict[str, Any]:
    rule = stored.rule
    return {
        "code": rule.code,
        "platform": rule.platform.value if rule.platform is not None else None,
        "entity_level": rule.entity_level.value,
        "action": rule.action_kind.value,
        "magnitude_pct": rule.magnitude_pct,
        "autonomy_level": rule.autonomy_level.value.upper(),
        "cooldown_seconds": int(rule.cooldown.total_seconds()),
        "enabled": stored.is_enabled,
    }


def account_gate_view(
    *, platform_account_id: str, label: str, confirmed: list[dict[str, Any]]
) -> dict[str, Any]:
    confirmed_keys = {item["key"] for item in confirmed}
    missing = [
        {"key": info.key, "label": info.label, "recommended_default": info.recommended_default}
        for info in GATE_KEYS
        if info.key not in confirmed_keys
    ]
    return {
        "platform_account_id": platform_account_id,
        "label": label,
        "ready": not missing,
        "missing": missing,
        "confirmed": confirmed,
    }


# ---------------------------------------------------------------------------
# `GET /rules`, `POST /rules`, `DELETE /rules/{id}`, `POST
# /rules/{id}/simulate`, `GET /guardrails` (contracts/rest-api.md §Reglas y
# guardarraíles lineas 219-229). Router propio -- necesita `Container`
# (sesion por peticion, igual que `composition/execution_rest.py`), pero se
# mantiene fuera de ese fichero a proposito (alcance de esta rama): vive en
# `rules.presentation`, el bounded context dueño de estas rutas, en vez de
# seguir creciendo el router de `execution`/`proposals`.
#
# `GET /rules` y `POST /rules` satisfacen `panel/src/api/schemas/
# rules.ts::ruleSchema` EXACTAMENTE, no la forma abreviada de
# `stored_rule_to_json` (esa es la de `PUT /rules/{id}`, ya cableada y
# fuera de esta rama) -- ver el informe de esta rama para el listado de
# discrepancias contrato-vs-zod.
# ---------------------------------------------------------------------------

_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]
_ScopedBusinessIdDep = Annotated[str, Depends(require_business_access)]
_RULE_CODE_PATTERN: Final = re.compile(r"^[MGX][0-9]{2}$")
_NOT_FOUND: Final = ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")
_MINOR_UNITS_PER_MAJOR: Final = 100


def build_rules_router(container: Container) -> APIRouter:  # noqa: PLR0915 - raiz de router, cada handler es de 5-10 lineas
    router = APIRouter(prefix="/api/v1", tags=["rules"])

    @router.get("/rules")
    async def list_rules(
        business_id: _ScopedBusinessIdDep,
        platform: PlatformCode | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        async with container.session_factory() as session:
            views = await list_rule_views(
                SqlRuleActivityReadPort(session),
                SqlRuleRepository(session),
                business_id=business_id,
                platform=platform,
                enabled=enabled,
                now=container.clock.now(),
            )
        return {"items": [_rule_view_to_json(view) for view in views]}

    @router.post("/rules", status_code=201)
    async def create_rule(
        business_id: _ScopedBusinessIdDep,
        owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        command = _parse_create_rule_body(body)
        async with container.session_factory() as session:
            stored = await _create_rule(session, command)
            await _record_decision(
                session,
                business_id=BusinessId.parse(business_id),
                actor_email=owner.email,
                kind=DecisionKind.RULE_CHANGE,
                payload={"event": "RuleCreated", "rule_code": stored.code},
            )
            await session.commit()
        return _rule_view_to_json(stored_rule_to_view(stored))

    @router.delete("/rules/{rule_code}", status_code=204)
    async def delete_rule(
        rule_code: str, business_id: _ScopedBusinessIdDep, owner: _OwnerDep
    ) -> None:
        async with container.session_factory() as session:
            rules = SqlRuleRepository(session)
            stored = await rules.get_by_code(rule_code)
            if stored is None:
                raise _NOT_FOUND
            await rules.set_autonomy(
                code=rule_code, level=stored.rule.autonomy_level, enabled=False
            )
            await _record_decision(
                session,
                business_id=BusinessId.parse(business_id),
                actor_email=owner.email,
                kind=DecisionKind.RULE_CHANGE,
                payload={"event": "RuleDeleted", "rule_code": rule_code},
            )
            await session.commit()

    @router.post("/rules/{rule_code}/simulate")
    async def simulate_rule(
        rule_code: str,
        business_id: _ScopedBusinessIdDep,
        body: Annotated[dict[str, Any], Body(default_factory=dict)],
    ) -> dict[str, Any]:
        entity_ref = _parse_optional_entity_ref(body.get("entity_ref"))
        async with container.session_factory() as session:
            stored = await SqlRuleRepository(session).get_by_code(rule_code)
            if stored is None:
                raise _NOT_FOUND
            if entity_ref is None:
                return _simulate_result_without_entity()
            entity = await SqlAdEntityRepository(session).get_by_ref(entity_ref)
            if entity is None or str(entity.business_id) != business_id:
                raise _NOT_FOUND
            return await _simulate_against_entity(session, stored.rule, entity)

    @router.get("/guardrails/setup")
    async def guardrails_setup(
        business_id: _ScopedBusinessIdDep, _owner: _OwnerDep
    ) -> dict[str, Any]:
        async with container.session_factory() as session:
            rows = await SqlGuardrailViewReadPort(session).list_setup_for_business(
                business_id=business_id
            )
        return {
            "items": [
                {**account, "guardrail": _guardrail_view_to_json(policy) if policy else None}
                for account, policy in rows
            ]
        }

    @router.get("/guardrails")
    async def list_guardrails(scope_ref: str, caller: CallerDep) -> dict[str, Any]:
        async with container.session_factory() as session:
            views = await _resolve_guardrail_views(session, scope_ref=scope_ref, caller=caller)
        return {"items": [_guardrail_view_to_json(view) for view in views]}

    return router


async def _create_rule(session: AsyncSession, command: CreateRuleCommand) -> StoredRule:
    try:
        return await CreateRule(SqlRuleRepository(session)).execute(command)
    except SpendIncreasingAutoRuleError as exc:
        raise ApiError(status_code=422, code="AUTO_WOULD_INCREASE_SPEND", message=str(exc)) from exc
    except (BlankRuleCodeError, InvalidCooldownError, EmptyConditionError) as exc:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc
    except DuplicateRuleCodeError as exc:
        raise ApiError(status_code=409, code="RULE_CODE_ALREADY_EXISTS", message=str(exc)) from exc


async def _record_decision(
    session: AsyncSession,
    *,
    business_id: BusinessId,
    actor_email: str,
    kind: DecisionKind,
    payload: dict[str, Any],
) -> None:
    recorder = RecordDecision(SqlDecisionLogRepository(session))
    await recorder.execute(
        PendingDecision(
            business_id=business_id,
            kind=kind,
            actor_kind=ActorKind.OWNER,
            actor_id=actor_email,
            payload=payload,
        )
    )


# ---------------------------------------------------------------------------
# `GET /rules` -- mapeo `RuleView` -> JSON (`ruleSchema`).
# ---------------------------------------------------------------------------


def _rule_view_to_json(view: RuleView) -> dict[str, Any]:
    return {
        "rule_id": view.rule_id,
        "code": view.code,
        "name": view.name,
        "scope": view.scope,
        "platform": view.platform.value if view.platform is not None else None,
        "condition_label": view.condition_label,
        "window": view.window,
        "action_label": view.action_label,
        "magnitude_pct": view.magnitude_pct,
        "autonomy_level": view.autonomy_level.value.upper(),
        "cooldown_hours": view.cooldown_hours,
        "is_enabled": view.is_enabled,
        "firings_30d": view.firings_30d,
        "hit_rate_pct": view.hit_rate_pct,
        "increases_spend": view.increases_spend,
    }


# ---------------------------------------------------------------------------
# `POST /rules` -- cuerpo (contracts/rest-api.md linea 220; sin zod que lo
# ate, el panel todavia no llama a esta ruta -- ver el informe de esta
# rama). `condition` viaja en la forma nativa del dominio
# (`{"clauses": [...]}`, mismos campos que `ConditionClause`): es la
# traduccion mas fiel sin inventar un DSL nuevo para 37 reglas que ya se
# cargan asi desde `rules.yaml`.
# ---------------------------------------------------------------------------


def _parse_create_rule_body(body: dict[str, Any]) -> CreateRuleCommand:
    return CreateRuleCommand(
        code=_parse_rule_code(body),
        platform=_parse_platform(body.get("platform")),
        entity_level=_parse_enum(EntityLevel, _require_str(body, "entity_level"), "entity_level"),
        description=_require_str(body, "description"),
        condition=_parse_condition(body.get("condition")),
        action_kind=_parse_enum(ActionKind, _require_str(body, "action"), "action"),
        magnitude_pct=_optional_number(body, "magnitude_pct"),
        autonomy_level=_parse_enum(
            AutonomyLevel, _require_str(body, "autonomy_level").lower(), "autonomy_level"
        ),
        cooldown=timedelta(hours=_require_number(body, "cooldown_hours")),
        source_url=str(body.get("source_url") or ""),
        enabled=bool(body.get("enabled", False)),
    )


def _parse_rule_code(body: dict[str, Any]) -> str:
    code = _require_str(body, "code")
    if not _RULE_CODE_PATTERN.match(code):
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"code invalido: {code!r} (formato [MGX] + 2 digitos)",
        )
    return code


def _parse_platform(raw: Any) -> PlatformCode | None:
    if raw is None:
        return None
    return _parse_enum(PlatformCode, raw, "platform")


def _parse_enum[EnumT: Enum](enum_type: type[EnumT], raw: Any, field: str) -> EnumT:
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message=f"{field} invalido: {raw!r}"
        ) from exc


def _parse_condition(raw: Any) -> Condition:
    if not isinstance(raw, dict) or not isinstance(raw.get("clauses"), list) or not raw["clauses"]:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="condition.clauses requiere al menos una clausula",
        )
    return Condition(clauses=tuple(_parse_clause(item) for item in raw["clauses"]))


def _parse_clause(raw: Any) -> ConditionClause:
    if not isinstance(raw, dict):
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="clausula de condition invalida"
        )
    return ConditionClause(
        metric=_require_str(raw, "metric"),
        comparator=_parse_enum(Comparator, _require_str(raw, "comparator"), "comparator"),
        window=_require_str(raw, "window"),
        threshold_kind=_parse_enum(
            ThresholdKind, _require_str(raw, "threshold_kind"), "threshold_kind"
        ),
        value=_require_number(raw, "value"),
    )


def _require_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=f"{key} requerido")
    return value


def _require_number(body: dict[str, Any], key: str) -> float:
    value = body.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=f"{key} requerido")
    return float(value)


def _optional_number(body: dict[str, Any], key: str) -> float | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=f"{key} invalido")
    return float(value)


# ---------------------------------------------------------------------------
# `POST /rules/{id}/simulate` -- ensayo en seco (contracts/rest-api.md
# linea 222): reevalua `rule_evaluator.evaluate_rule`/`evaluate_creative_
# rule` (los mismos que usa `orchestration.infrastructure.rule_step`, una
# sola verdad) contra la ultima senal viva de la entidad, sin escribir
# nada -- ninguna llamada de esta rama toca `session.commit()`. La
# autonomia/habilitada de la regla NO filtra `would_fire` a proposito: es
# una vista previa ("si la encendiera, ?dispararia?"), no una
# autorizacion real (esa la sigue haciendo `SqlRuleConditionPort`, que si
# exige AUTO+habilitada+datos frescos).
# ---------------------------------------------------------------------------


def _parse_optional_entity_ref(raw: Any) -> EntityRef | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="entity_ref debe ser una cadena"
        )
    try:
        return EntityRef.parse(raw)
    except EntityRefFormatError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message=f"entity_ref invalido: {raw!r}"
        ) from exc


def _simulate_result_without_entity() -> dict[str, Any]:
    return {
        "would_fire": False,
        "reason": "Falta entity_ref: no se puede evaluar la condicion sin una entidad.",
        "projected_diff": None,
    }


async def _simulate_against_entity(
    session: AsyncSession, rule: Rule, entity: AdEntity
) -> dict[str, Any]:
    cycle_id = uuid.uuid4()
    signals = SqlSignalRepository(session, cycle_id=cycle_id)
    signal = await signals.find_latest_for_entity(entity_ref=entity.entity_ref)
    if signal is not None:
        outcome = evaluate_rule(rule, signal)
        if outcome is not RuleOutcome.NOOP:
            return _fires_result(rule, entity, outcome, signal.kind.value)
    creative_signals = SqlCreativeSignalRepository(session, cycle_id=cycle_id)
    creative = await creative_signals.find_latest_for_entity(entity_ref=entity.entity_ref)
    if creative is not None:
        outcome = evaluate_creative_rule(rule, creative)
        if outcome is not RuleOutcome.NOOP:
            return _fires_result(rule, entity, outcome, creative.kind.value)
    return {
        "would_fire": False,
        "reason": "Sin senal reciente que coincida con esta regla para esta entidad.",
        "projected_diff": None,
    }


def _fires_result(
    rule: Rule, entity: AdEntity, outcome: RuleOutcome, signal_kind: str
) -> dict[str, Any]:
    reason = (
        f"La condicion se cumple: ultima senal «{signal_kind}» -> "
        f"{outcome.value} (autonomia {rule.autonomy_level.value})."
    )
    return {"would_fire": True, "reason": reason, "projected_diff": _projected_diff(rule, entity)}


_BUDGET_ACTIONS: Final = frozenset({ActionKind.BUY, ActionKind.SELL})


def _projected_diff(rule: Rule, entity: AdEntity) -> dict[str, Any] | None:
    """Solo `BUY`/`SELL` tienen una traduccion mecanica a presupuesto (mismo
    catalogo de acciones "con antes/despues" que
    `orchestration.infrastructure.rule_step._budget_diff`, reescrito aqui en
    vez de importado: esa funcion es privada de `orchestration` y esta rama
    no toca ese modulo)."""
    if rule.action_kind not in _BUDGET_ACTIONS or rule.magnitude_pct is None:
        return None
    if entity.budget is None:
        return None
    before_minor = entity.budget.amount.minor_units
    fraction = Decimal(str(rule.magnitude_pct)) / Decimal(100)
    factor = Decimal(1) - fraction if rule.action_kind is ActionKind.SELL else Decimal(1) + fraction
    after_minor = int((Decimal(before_minor) * factor).to_integral_value(rounding=ROUND_HALF_UP))
    return {
        "parametro": "daily_budget",
        "valor_actual": before_minor / _MINOR_UNITS_PER_MAJOR,
        "valor_propuesto": after_minor / _MINOR_UNITS_PER_MAJOR,
    }


# ---------------------------------------------------------------------------
# `GET /guardrails?scope_ref` -- lado de lectura de `PUT /guardrails/{id}`.
# `scope_ref` no tiene un `Depends` propio (no se llama `business_id`): se
# resuelve a mano, mismo criterio fail-closed que el resto del contrato
# (404, nunca 403 -- `ensure_business_access` + existencia real).
# ---------------------------------------------------------------------------


_FIND_ACCOUNT_BUSINESS: Final = text(
    "SELECT business_id FROM platform_accounts WHERE account_ref = :account_ref"
)


async def _resolve_guardrail_views(
    session: AsyncSession, *, scope_ref: str, caller: AuthenticatedCaller
) -> list[GuardrailView]:
    try:
        kind, ref = parse_scope_ref(scope_ref)
    except InvalidScopeRefError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message=f"scope_ref invalido: {scope_ref!r}"
        ) from exc
    if kind == "business":
        return await _guardrails_for_business(session, business_id=ref, caller=caller)
    return await _guardrails_for_account(session, account_ref=ref, caller=caller)


async def _guardrails_for_business(
    session: AsyncSession, *, business_id: str, caller: AuthenticatedCaller
) -> list[GuardrailView]:
    ensure_business_access(business_id, caller)
    exists = await SqlBusinessDirectory(session).exists(uuid.UUID(business_id))
    if not exists:
        raise _NOT_FOUND
    return await SqlGuardrailViewReadPort(session).list_for_business(business_id=business_id)


async def _guardrails_for_account(
    session: AsyncSession, *, account_ref: str, caller: AuthenticatedCaller
) -> list[GuardrailView]:
    platform, _, external_account_id = account_ref.partition(":")
    business_id = await _business_id_for_account(
        session, platform=platform, external_account_id=external_account_id
    )
    if business_id is None:
        raise _NOT_FOUND
    ensure_business_access(str(business_id), caller)
    return await SqlGuardrailViewReadPort(session).list_for_account(
        platform=platform, external_account_id=external_account_id
    )


async def _business_id_for_account(
    session: AsyncSession, *, platform: str, external_account_id: str
) -> uuid.UUID | None:
    result = await session.execute(
        _FIND_ACCOUNT_BUSINESS,
        {"account_ref": f"{platform}:{external_account_id}"},
    )
    row = result.one_or_none()
    return None if row is None else row.business_id


def _guardrail_view_to_json(view: GuardrailView) -> dict[str, Any]:
    return {
        "guardrail_id": view.guardrail_id,
        "scope": view.scope,
        "scope_label": view.scope_label,
        "daily_cap": view.daily_cap,
        "monthly_cap": view.monthly_cap,
        "budget_floor": view.budget_floor,
        "budget_ceiling": view.budget_ceiling,
        "max_step_pct": view.max_step_pct,
        "max_changes_per_entity_per_day": view.max_changes_per_entity_per_day,
        "min_viable_spend": view.min_viable_spend,
        "currency": view.currency,
    }
