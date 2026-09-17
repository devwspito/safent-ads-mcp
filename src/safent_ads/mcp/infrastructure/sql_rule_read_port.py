"""`RuleReadPort` real (integracion, wiring2) sobre `rules`/`guardrails`/
`emergency_brakes` (`0007_rules_guardrails.py`, `0012_domain_alignment.py`).

Reutiliza los repositorios SQL reales de `rules` donde la forma ya encaja
(`SqlRuleRepository.get_by_code`, `SqlGuardrailRepository.find_for_account`,
`SqlEmergencyBrakeRepository.find_active`, mismo patron que
`panel.infrastructure.sql_read_model._caps_and_pacing`/`get_badges` usan los
dos ultimos). `list_rules` necesita ver tambien las reglas deshabilitadas
(el repositorio solo expone `list_enabled`), asi que esta clase anade su
propia consulta de proyeccion -- igual que `panel` escribe sus propias
consultas en vez de forzar la forma de escritura de otro contexto.

`rules` vive en `scope = 'global'`: ninguna fila lleva `business_id` todavia
(data-model.md), asi que `list_rules`/`get_rule` no filtran por negocio --
el parametro se acepta por contrato (`contracts/mcp-tools.md`) pero el
catalogo es el mismo para todos los negocios hasta que exista calibracion
por negocio. `list_guardrails`/`explain_rule` SI cruzan con `business_id`
(via `platform_accounts`/`ad_entities`) porque ahi la fila si es de un
negocio concreto -- una cuenta o entidad de otro negocio nunca debe
responder (IDOR, mismo principio que `mcp/testing/fakes.py`)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp.application.dto import (
    AutonomyLevel,
    GuardrailInfo,
    KillSwitchStatus,
    PlatformCode,
    RuleDetail,
    RuleExplanation,
    RuleSummary,
)
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.rules.domain.condition import Condition
from safent_ads.rules.domain.emergency_brake import BrakeScope, BrakeScopeKind
from safent_ads.rules.domain.rule_evaluator import RuleOutcome, evaluate_rule
from safent_ads.rules.domain.stored_rule import StoredRule
from safent_ads.rules.infrastructure.sql_repositories import (
    SqlEmergencyBrakeRepository,
    SqlGuardrailRepository,
    SqlRuleRepository,
)
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.shared.read_models.dto import Money
from safent_ads.signals.infrastructure.sql_repositories import SqlSignalRepository

_MINOR_UNITS_PER_MAJOR = 100
# `find_latest_for_entity` no escribe: el `cycle_id` del constructor solo
# importa para `save()`, que este puerto de lectura nunca llama.
_ZERO_CYCLE = uuid.UUID(int=0)

_SELECT_RULES = text("""
    SELECT code, platform, is_enabled, autonomy_level
      FROM rules
     WHERE scope = 'global'
       AND (CAST(:platform AS TEXT) IS NULL
            OR platform IS NOT DISTINCT FROM CAST(:platform AS TEXT))
       AND (CAST(:enabled AS BOOLEAN) IS NULL OR is_enabled = CAST(:enabled AS BOOLEAN))
     ORDER BY code
""")

_SELECT_ENTITY_BUSINESS = text("SELECT business_id FROM ad_entities WHERE entity_ref = :entity_ref")

_SELECT_GUARDRAIL_CURRENCY = text("""
    SELECT guardrail.currency
      FROM guardrails AS guardrail
      JOIN platform_accounts AS account ON account.id = guardrail.platform_account_id
     WHERE guardrail.scope = 'platform_account'
       AND account.business_id = :business_id
       AND account.account_ref = :account_ref
""")


class SqlRuleReadPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_rules(
        self, business_id: str, *, platform: str | None, enabled: bool | None
    ) -> list[RuleSummary]:
        del business_id  # rules.scope = 'global': catalogo compartido (ver docstring)
        async with self._session_factory() as session:
            rows = (
                (await session.execute(_SELECT_RULES, {"platform": platform, "enabled": enabled}))
                .mappings()
                .all()
            )
        return [_summary(row) for row in rows]

    async def get_rule(self, business_id: str, rule_id: str) -> RuleDetail:
        del business_id
        async with self._session_factory() as session:
            stored = await SqlRuleRepository(session).get_by_code(rule_id)
        if stored is None:
            raise EntityNotFoundError(f"{rule_id} aun no disponible")
        return _detail(stored)

    async def explain_rule(
        self, business_id: str, rule_id: str, *, entity_ref: str | None
    ) -> RuleExplanation:
        async with self._session_factory() as session:
            stored = await SqlRuleRepository(session).get_by_code(rule_id)
            if stored is None:
                raise EntityNotFoundError(f"{rule_id} aun no disponible")
            if entity_ref is None:
                return RuleExplanation(
                    rule_id=rule_id,
                    would_fire=False,
                    reason="sin entity_ref: no se puede evaluar la condicion sobre una senal",
                    projected_diff={},
                )
            ref = EntityRef.parse(entity_ref)
            owner = (
                await session.execute(_SELECT_ENTITY_BUSINESS, {"entity_ref": str(ref)})
            ).scalar_one_or_none()
            if owner is None or str(owner) != business_id:
                raise EntityNotFoundError(f"{entity_ref} aun no disponible")
            signal_repo = SqlSignalRepository(session, cycle_id=_ZERO_CYCLE)
            signal = await signal_repo.find_latest_for_entity(entity_ref=ref)
        if signal is None:
            return RuleExplanation(
                rule_id=rule_id,
                would_fire=False,
                reason=f"{entity_ref} no tiene ninguna senal emitida todavia",
                projected_diff={},
            )
        outcome = evaluate_rule(stored.rule, signal)
        would_fire = outcome is not RuleOutcome.NOOP
        reason = (
            signal.cause_sentence
            if would_fire
            else (f"la ultima senal de {entity_ref} no coincide con la condicion de {rule_id}")
        )
        projected_diff = (
            {"action": stored.rule.action_kind.value, "outcome": outcome.value}
            if would_fire
            else {}
        )
        return RuleExplanation(
            rule_id=rule_id, would_fire=would_fire, reason=reason, projected_diff=projected_diff
        )

    async def list_guardrails(self, business_id: str, scope_ref: str) -> list[GuardrailInfo]:
        platform, external_account_id = _split_account_ref(scope_ref)
        async with self._session_factory() as session:
            policy = await SqlGuardrailRepository(session).find_for_account(account_ref=scope_ref)
            if policy is None:
                return []
            currency = (
                await session.execute(
                    _SELECT_GUARDRAIL_CURRENCY,
                    {
                        "business_id": business_id,
                        "platform": platform,
                        "external_account_id": external_account_id,
                        "account_ref": scope_ref,
                    },
                )
            ).scalar_one_or_none()
        if currency is None:
            # La cuenta existe pero no es de este negocio (o no tiene guardarrail
            # completo): mismo tratamiento IDOR-safe que un `EntityNotFoundError`,
            # sin exponer si la cuenta existe en otro negocio.
            return []
        return [
            GuardrailInfo(
                scope_ref=scope_ref,
                daily_cap=_money(policy.daily_cap_minor, currency),
                monthly_cap=_money(policy.monthly_cap_minor, currency),
                budget_floor=_money(policy.floor_minor, currency),
                budget_ceiling=_money(policy.ceiling_minor, currency),
                max_step_pct=policy.max_step_pct,
                max_changes_per_entity_per_day=policy.max_changes_per_day,
            )
        ]

    async def get_kill_switch_status(self, business_id: str) -> KillSwitchStatus:
        async with self._session_factory() as session:
            brakes = SqlEmergencyBrakeRepository(session)
            global_brake = await brakes.find_active(scope=BrakeScope(kind=BrakeScopeKind.GLOBAL))
            business_brake = global_brake or await brakes.find_active(
                scope=BrakeScope(
                    kind=BrakeScopeKind.BUSINESS, business_id=BusinessId.parse(business_id)
                )
            )
        if business_brake is None:
            return KillSwitchStatus(
                engaged=False, scope="global", mode="ALL", reason=None, since=None
            )
        scope = "global" if global_brake is not None else "business"
        return KillSwitchStatus(
            engaged=True,
            scope=scope,
            mode=business_brake.mode.value.upper(),
            reason=business_brake.reason,
            since=business_brake.engaged_at,
        )


def _split_account_ref(account_ref: str) -> tuple[str, str]:
    platform, external_account_id = account_ref.split(":", 1)
    return platform, external_account_id


def _money(minor: int, currency: str) -> Money:
    return Money(Decimal(minor) / _MINOR_UNITS_PER_MAJOR, currency)


def _summary(row: RowMapping) -> RuleSummary:
    return RuleSummary(
        rule_id=row["code"],
        code=row["code"],
        platform=PlatformCode(row["platform"]) if row["platform"] is not None else None,
        enabled=row["is_enabled"],
        autonomy_level=AutonomyLevel(row["autonomy_level"].lower()),
    )


def _detail(stored: StoredRule) -> RuleDetail:
    rule = stored.rule
    summary = RuleSummary(
        rule_id=rule.code,
        code=rule.code,
        platform=PlatformCode(rule.platform.value) if rule.platform is not None else None,
        enabled=stored.is_enabled,
        autonomy_level=AutonomyLevel(rule.autonomy_level.value),
    )
    return RuleDetail(
        summary=summary,
        condition=_render_condition(rule.condition),
        window_label=rule.condition.clauses[0].window,
        action=rule.action_kind.value,
        magnitude_pct=rule.magnitude_pct,
        cooldown_hours=int(rule.cooldown.total_seconds() // 3600),
    )


def _render_condition(condition: Condition) -> str:
    return " AND ".join(
        f"{clause.metric} {clause.comparator.value} {clause.value}"
        f" ({clause.threshold_kind.value}, {clause.window})"
        for clause in condition.clauses
    )
