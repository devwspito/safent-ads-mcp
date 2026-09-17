"""`ProposalWritePort` (mcp.application) sobre `Container.build_execution_use_cases`:
el puente entre la superficie MCP y el camino de escritura real. Vive aqui,
no en `mcp/infrastructure/`, porque `Container` es la raiz de composicion
(transversal) -- `mcp` no puede importarla sin invertir el grafo de
plan.md §4.

Abre su propia sesion por llamada (mismo patron que `RequestScopedPanelReadPort`/
`_PerCall*` de `orchestration/infrastructure/runtime.py`): cada tool MCP es
su propia transaccion, confirmada solo si el caso de uso completo (incluida
la escritura del bróker, para `apply_defensive_action`) no lanzo."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.budget import BudgetKind
from safent_ads.accounts.infrastructure.sql_repositories import SqlAdEntityRepository
from safent_ads.composition.container import Container
from safent_ads.execution.application.apply_defensive_action import (
    ApplyDefensiveActionCommand,
    ApplyDefensiveActionDeniedError,
    UnsupportedDefensiveActionError,
)
from safent_ads.execution.application.authorize_rule_action import RuleAuthorizationDeniedError
from safent_ads.execution.domain.defensive_actions import (
    DefensiveActionDenialCode,
    DefensiveActionKind,
)
from safent_ads.mcp.application.errors import (
    BrakeEngagedError,
    GuardrailBlockedError,
    RuleNotApplicableError,
    StaleDataError,
)
from safent_ads.mcp.application.errors import ToolValidationError as McpValidationError
from safent_ads.mcp.application.proposal_write_port import (
    DefensiveActionResult,
    ProposalWriteResult,
    WithdrawProposalResult,
)
from safent_ads.proposals.application.propose_action import (
    ProposeActionCommand,
    ProposeActionResult,
)
from safent_ads.proposals.application.withdraw_proposal import (
    ProposalNotWithdrawableError,
    WithdrawProposalCommand,
)
from safent_ads.proposals.domain.ad_child_creation import validate_child_payload
from safent_ads.proposals.domain.cause import Cause, Evidence
from safent_ads.proposals.domain.classification import ProposalKind
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.priority import Urgency
from safent_ads.proposals.domain.proposal import ProposedDiff
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef
from safent_ads.shared.managed_ads import ManagedAdsBinding

__all__ = ["ContainerProposalWriteAdapter"]

_BUDGET_PARAMETER = "daily_budget"
_STATUS_PARAMETER = "status"
_TARGETING_PARAMETER = "targeting"
# H-1 (004 tasks-2.md §1): `parameter="creative_publication"` no tiene
# operacion en `execution/infrastructure/broker_platform.py::
# _OPERATION_BY_PARAMETER` -- una propuesta aprobada moria en
# `UnsupportedWriteParameterError`. Publicar creatividad y "rotar fuera"
# una creatividad (pausarla) NO son la misma mutacion de Meta
# (`_MUTATE_FIELDS_BY_OPERATION[ROTATE_OUT_CREATIVE]` pone `status=PAUSED`,
# nada que ver con adjuntar `creative_asset_ids`/`ad_copy`) -- decision
# tomada con el test delante (tests/unit/execution/
# test_operation_por_parametro.py): el mapa NO cambia, esta herramienta
# pasa a escribir el parametro que el mapa ya conoce.
_CREATIVE_PARAMETER = "creative"
_BID_TARGET_PARAMETER = "bid_target"
_NEGATIVE_KEYWORDS_PARAMETER = "negative_keywords"
_DELETED_STATUS_VALUE = "DELETED"  # vocabulario canonico de dominio, no el de ninguna plataforma
_NATIVE_WRITE_PARAMETER_TEMPLATE = "native:{platform}:{operation}"
_MINOR_UNITS_PER_MAJOR = Decimal(100)
_RESUMABLE_STATUSES = frozenset({AdEntityStatus.PAUSED})
_DELETABLE_STATUSES = frozenset(
    {AdEntityStatus.ACTIVE, AdEntityStatus.PAUSED, AdEntityStatus.LEARNING, AdEntityStatus.DRIFTED}
)

_DEFENSIVE_ACTION_DENIAL_TO_MCP_ERROR = {
    DefensiveActionDenialCode.BRAKE_ENGAGED: BrakeEngagedError,
    DefensiveActionDenialCode.STALE_DATA: StaleDataError,
    DefensiveActionDenialCode.RULE_NOT_APPLICABLE: RuleNotApplicableError,
    DefensiveActionDenialCode.VALIDATION_ERROR: McpValidationError,
    DefensiveActionDenialCode.GUARDRAIL_BLOCKED: GuardrailBlockedError,
}


class ContainerProposalWriteAdapter:
    def __init__(
        self, container: Container, *, managed_binding: ManagedAdsBinding | None = None
    ) -> None:
        self._container = container
        self._managed_binding = managed_binding

    async def propose_ad_child(
        self,
        *,
        business_id: str,
        entity_ref: str,
        child_plan: dict[str, object],
        cause_text: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult:
        ref = EntityRef.parse(entity_ref)
        business = BusinessId.parse(business_id)
        if ref.business_id != business.value or ref.connection_id is None:
            raise McpValidationError("La entidad padre no pertenece al negocio/conexión.")
        plan = validate_child_payload({"child_plan": child_plan}, ref)
        async with self._container.session_factory() as session:
            entity = await self._require_entity(session, ref)
            active = (
                await session.execute(
                    text("""SELECT 1 FROM ad_entities e
                JOIN platform_accounts a ON a.id=e.platform_account_id
                JOIN platform_connections c ON c.id=a.connection_id
                WHERE e.entity_ref=:ref AND a.business_id=:business AND a.status='ACTIVE'
                """),
                    {"ref": str(ref), "business": business.value},
                )
            ).scalar_one_or_none()
            if active is None:
                raise McpValidationError("La cuenta/conexión padre no está disponible.")
            suffix = hashlib.sha256(
                json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest()
            diff = ProposedDiff.build(
                entity_ref=ref,
                parameter=f"new_{plan['kind']}:{suffix[:32]}",
                before=None,
                after={"child_plan": plan},
                managed_binding=self._managed_binding,
            )
            cases = self._container.build_execution_use_cases(session)
            result = await cases.propose_action.execute(
                ProposeActionCommand(
                    business_id=business,
                    diff=diff,
                    kind=ProposalKind(f"create_{plan['kind']}"),
                    cause=Cause(text=cause_text),
                    cause_type="agent_ad_child_creation",
                    evidence=(),
                    estimated_impact=Money.zero("EUR"),
                    urgency=Urgency.RECOMMENDED,
                    expected_state_hash=entity.platform_state_hash.value,
                    proposed_by=proposed_by,
                )
            )
            await session.commit()
        return _to_dto(result)

    async def propose_budget_change(
        self,
        *,
        business_id: str,
        entity_ref: str,
        new_daily_budget_amount: str,
        new_daily_budget_currency: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult:
        ref = EntityRef.parse(entity_ref)
        after = Money.of(new_daily_budget_amount, new_daily_budget_currency)
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            entity = await self._require_entity(session, ref)
            if entity.budget is None or entity.budget.kind is not BudgetKind.DAILY:
                raise McpValidationError("La entidad no tiene un presupuesto diario propio.")
            before = _current_budget(entity, after.currency)
            kind = ProposalKind.BUDGET_INCREASE if after > before else ProposalKind.BUDGET_DECREASE
            diff = ProposedDiff.build(
                entity_ref=ref,
                parameter=_BUDGET_PARAMETER,
                before=before,
                after=after,
                managed_binding=self._managed_binding,
            )
            result = await use_cases.propose_action.execute(
                ProposeActionCommand(
                    business_id=BusinessId.parse(business_id),
                    diff=diff,
                    kind=kind,
                    cause=Cause(text=cause_text, signal_id=cause_signal_id, rule_id=cause_rule_id),
                    cause_type="agent_budget_change",
                    evidence=_to_evidence(evidence),
                    estimated_impact=_impact(before, after),
                    urgency=Urgency(urgency),
                    expected_state_hash=entity.platform_state_hash.value,
                    proposed_by=proposed_by,
                )
            )
            await session.commit()
        return _to_dto(result)

    async def propose_pause(
        self,
        *,
        business_id: str,
        entity_ref: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult:
        ref = EntityRef.parse(entity_ref)
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            entity = await self._require_entity(session, ref)
            before, after = _pause_values(entity)
            currency = entity.budget.amount.currency if entity.budget is not None else "EUR"
            impact = _current_budget(entity, currency)
            diff = ProposedDiff.build(
                entity_ref=ref,
                parameter=_STATUS_PARAMETER,
                before=before,
                after=after,
                managed_binding=self._managed_binding,
            )
            result = await use_cases.propose_action.execute(
                ProposeActionCommand(
                    business_id=BusinessId.parse(business_id),
                    diff=diff,
                    kind=ProposalKind.PAUSE,
                    cause=Cause(text=cause_text, signal_id=cause_signal_id, rule_id=cause_rule_id),
                    cause_type="agent_pause",
                    evidence=_to_evidence(evidence),
                    estimated_impact=impact,
                    urgency=Urgency(urgency),
                    expected_state_hash=entity.platform_state_hash.value,
                    proposed_by=proposed_by,
                )
            )
            await session.commit()
        return _to_dto(result)

    async def propose_targeting_change(
        self,
        *,
        business_id: str,
        entity_ref: str,
        targeting_diff: dict[str, object],
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult:
        ref = EntityRef.parse(entity_ref)
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            entity = await self._require_entity(session, ref)
            diff = ProposedDiff.build(
                entity_ref=ref, parameter=_TARGETING_PARAMETER, before=None, after=targeting_diff
            )
            result = await use_cases.propose_action.execute(
                ProposeActionCommand(
                    business_id=BusinessId.parse(business_id),
                    diff=diff,
                    kind=ProposalKind.TARGETING_CHANGE,
                    cause=Cause(text=cause_text, signal_id=cause_signal_id, rule_id=cause_rule_id),
                    cause_type="agent_targeting_change",
                    evidence=_to_evidence(evidence),
                    estimated_impact=Money.zero(),
                    urgency=Urgency(urgency),
                    expected_state_hash=entity.platform_state_hash.value,
                    proposed_by=proposed_by,
                )
            )
            await session.commit()
        return _to_dto(result)

    async def propose_creative_publication(
        self,
        *,
        business_id: str,
        ad_set_ref: str,
        creative_asset_ids: tuple[str, ...],
        ad_copy: dict[str, object],
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult:
        ref = EntityRef.parse(ad_set_ref)
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            entity = await self._require_entity(session, ref)
            after: dict[str, object] = {
                "creative_asset_ids": list(creative_asset_ids),
                "ad_copy": ad_copy,
            }
            diff = ProposedDiff.build(
                entity_ref=ref, parameter=_CREATIVE_PARAMETER, before=None, after=after
            )
            result = await use_cases.propose_action.execute(
                ProposeActionCommand(
                    business_id=BusinessId.parse(business_id),
                    diff=diff,
                    kind=ProposalKind.CREATIVE_PUBLICATION,
                    cause=Cause(text=cause_text, signal_id=cause_signal_id, rule_id=cause_rule_id),
                    cause_type="agent_creative_publication",
                    evidence=(),
                    estimated_impact=Money.zero(),
                    urgency=Urgency.RECOMMENDED,
                    expected_state_hash=entity.platform_state_hash.value,
                    proposed_by=proposed_by,
                )
            )
            await session.commit()
        return _to_dto(result)

    async def propose_resume(
        self,
        *,
        business_id: str,
        entity_ref: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult:
        ref = EntityRef.parse(entity_ref)
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            entity = await self._require_entity(session, ref)
            before, after = _status_diff(
                entity, allowed=_RESUMABLE_STATUSES, target=AdEntityStatus.ACTIVE.value
            )
            diff = ProposedDiff.build(
                entity_ref=ref, parameter=_STATUS_PARAMETER, before=before, after=after
            )
            result = await use_cases.propose_action.execute(
                ProposeActionCommand(
                    business_id=BusinessId.parse(business_id),
                    diff=diff,
                    kind=ProposalKind.RESUME,
                    cause=Cause(text=cause_text, signal_id=cause_signal_id, rule_id=cause_rule_id),
                    cause_type="agent_resume",
                    evidence=_to_evidence(evidence),
                    estimated_impact=_current_budget(entity, _currency_of(entity)),
                    urgency=Urgency(urgency),
                    expected_state_hash=entity.platform_state_hash.value,
                    proposed_by=proposed_by,
                )
            )
            await session.commit()
        return _to_dto(result)

    async def propose_bid_target(
        self,
        *,
        business_id: str,
        entity_ref: str,
        bid_target_amount: str,
        bid_target_currency: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult:
        ref = EntityRef.parse(entity_ref)
        after = Money.of(bid_target_amount, bid_target_currency)
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            entity = await self._require_entity(session, ref)
            diff = ProposedDiff.build(
                entity_ref=ref,
                parameter=_BID_TARGET_PARAMETER,
                before=_current_bid_target(entity),
                after=after,
            )
            result = await use_cases.propose_action.execute(
                ProposeActionCommand(
                    business_id=BusinessId.parse(business_id),
                    diff=diff,
                    kind=ProposalKind.BID_TARGET,
                    cause=Cause(text=cause_text, signal_id=cause_signal_id, rule_id=cause_rule_id),
                    cause_type="agent_bid_target_change",
                    evidence=_to_evidence(evidence),
                    estimated_impact=Money.zero(),
                    urgency=Urgency(urgency),
                    expected_state_hash=entity.platform_state_hash.value,
                    proposed_by=proposed_by,
                )
            )
            await session.commit()
        return _to_dto(result)

    async def propose_negative_keywords(
        self,
        *,
        business_id: str,
        entity_ref: str,
        keywords: tuple[str, ...],
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult:
        ref = EntityRef.parse(entity_ref)
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            entity = await self._require_entity(session, ref)
            diff = ProposedDiff.build(
                entity_ref=ref,
                parameter=_NEGATIVE_KEYWORDS_PARAMETER,
                before=None,
                after=list(keywords),
            )
            result = await use_cases.propose_action.execute(
                ProposeActionCommand(
                    business_id=BusinessId.parse(business_id),
                    diff=diff,
                    kind=ProposalKind.NEGATIVE_KEYWORD,
                    cause=Cause(text=cause_text, signal_id=cause_signal_id, rule_id=cause_rule_id),
                    cause_type="agent_negative_keywords",
                    evidence=_to_evidence(evidence),
                    estimated_impact=Money.zero(),
                    urgency=Urgency(urgency),
                    expected_state_hash=entity.platform_state_hash.value,
                    proposed_by=proposed_by,
                )
            )
            await session.commit()
        return _to_dto(result)

    async def propose_creative_rotation(
        self,
        *,
        business_id: str,
        entity_ref: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult:
        ref = EntityRef.parse(entity_ref)
        if ref.level is not EntityLevel.AD:
            raise McpValidationError(
                "Rotar una creatividad fuera solo se propone sobre un anuncio."
            )
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            entity = await self._require_entity(session, ref)
            before, after = _pause_values(entity)
            diff = ProposedDiff.build(
                entity_ref=ref, parameter=_CREATIVE_PARAMETER, before=before, after=after
            )
            result = await use_cases.propose_action.execute(
                ProposeActionCommand(
                    business_id=BusinessId.parse(business_id),
                    diff=diff,
                    kind=ProposalKind.ROTATE_OUT_CREATIVE,
                    cause=Cause(text=cause_text, signal_id=cause_signal_id, rule_id=cause_rule_id),
                    cause_type="agent_creative_rotation",
                    evidence=_to_evidence(evidence),
                    estimated_impact=Money.zero(),
                    urgency=Urgency(urgency),
                    expected_state_hash=entity.platform_state_hash.value,
                    proposed_by=proposed_by,
                )
            )
            await session.commit()
        return _to_dto(result)

    async def propose_delete(
        self,
        *,
        business_id: str,
        entity_ref: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        evidence: tuple[tuple[str, float, float, str], ...],
        urgency: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult:
        ref = EntityRef.parse(entity_ref)
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            entity = await self._require_entity(session, ref)
            before, after = _status_diff(
                entity, allowed=_DELETABLE_STATUSES, target=_DELETED_STATUS_VALUE
            )
            diff = ProposedDiff.build(
                entity_ref=ref, parameter=_STATUS_PARAMETER, before=before, after=after
            )
            result = await use_cases.propose_action.execute(
                ProposeActionCommand(
                    business_id=BusinessId.parse(business_id),
                    diff=diff,
                    kind=ProposalKind.DELETE,
                    cause=Cause(text=cause_text, signal_id=cause_signal_id, rule_id=cause_rule_id),
                    cause_type="agent_delete",
                    evidence=_to_evidence(evidence),
                    estimated_impact=_current_budget(entity, _currency_of(entity)),
                    urgency=Urgency(urgency),
                    expected_state_hash=entity.platform_state_hash.value,
                    proposed_by=proposed_by,
                )
            )
            await session.commit()
        return _to_dto(result)

    async def propose_native_write(
        self,
        *,
        business_id: str,
        entity_ref: str,
        platform: str,
        operation: str,
        payload: dict[str, object],
        why: str,
        proposed_by: str | None = None,
    ) -> ProposalWriteResult:
        ref = EntityRef.parse(entity_ref)
        parameter = _NATIVE_WRITE_PARAMETER_TEMPLATE.format(platform=platform, operation=operation)
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            entity = await self._require_entity(session, ref)
            diff = ProposedDiff.build(
                entity_ref=ref, parameter=parameter, before=None, after=payload
            )
            result = await use_cases.propose_action.execute(
                ProposeActionCommand(
                    business_id=BusinessId.parse(business_id),
                    diff=diff,
                    kind=ProposalKind.NATIVE_WRITE,
                    cause=Cause(text=why),
                    cause_type="agent_native_write",
                    evidence=(),
                    estimated_impact=Money.zero(),
                    urgency=Urgency.RECOMMENDED,
                    expected_state_hash=entity.platform_state_hash.value,
                    proposed_by=proposed_by,
                )
            )
            await session.commit()
        return _to_dto(result)

    async def withdraw_proposal(
        self, *, business_id: str, proposal_id: str, reason: str | None
    ) -> WithdrawProposalResult:
        del business_id  # IDOR ya lo comprobo el dispatcher (business_id_of); ver args.py
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            try:
                state = await use_cases.withdraw_proposal.execute(
                    WithdrawProposalCommand(ProposalId.parse(proposal_id), reason)
                )
            except ProposalNotWithdrawableError as exc:
                raise McpValidationError(str(exc)) from exc
            await session.commit()
        return WithdrawProposalResult(estado=state.value)

    async def apply_defensive_action(
        self,
        *,
        business_id: str,
        entity_ref: str,
        rule_id: str,
        action: str,
        cause_text: str,
        cause_signal_id: str | None,
        cause_rule_id: str | None,
        magnitude_pct: float | None,
    ) -> DefensiveActionResult:
        async with self._container.session_factory() as session:
            use_cases = self._container.build_execution_use_cases(session)
            try:
                result = await use_cases.apply_defensive_action.execute(
                    ApplyDefensiveActionCommand(
                        business_id=BusinessId.parse(business_id),
                        entity_ref=EntityRef.parse(entity_ref),
                        rule_id=rule_id,
                        action=DefensiveActionKind(action),
                        cause=Cause(
                            text=cause_text, signal_id=cause_signal_id, rule_id=cause_rule_id
                        ),
                        magnitude_pct=magnitude_pct,
                    )
                )
            except ApplyDefensiveActionDeniedError as exc:
                raise _DEFENSIVE_ACTION_DENIAL_TO_MCP_ERROR[exc.code](str(exc)) from exc
            except (RuleAuthorizationDeniedError, UnsupportedDefensiveActionError) as exc:
                raise McpValidationError(str(exc)) from exc
            await session.commit()
        return DefensiveActionResult(
            execution_id=str(result.execution_id),
            outcome=result.outcome.value,
            applied_value=result.applied_value,
            undo_deadline=result.undo_deadline,
        )

    async def _require_entity(self, session: AsyncSession, entity_ref: EntityRef) -> AdEntity:
        if self._managed_binding is not None:
            exists = await session.execute(
                text("""SELECT 1 FROM ad_entities e
                JOIN platform_accounts a ON a.id=e.platform_account_id
                WHERE e.entity_ref=:entity AND a.account_ref=:account AND a.status='ACTIVE'"""),
                {"entity": str(entity_ref), "account": str(self._managed_binding.provider_account)},
            )
            if exists.scalar_one_or_none() is None:
                raise McpValidationError("Entidad no disponible para esta cuenta")
        entity = await SqlAdEntityRepository(session).get_by_ref(entity_ref)
        if entity is None:
            raise McpValidationError(f"{entity_ref} desconocida")
        return entity


def _to_evidence(rows: tuple[tuple[str, float, float, str], ...]) -> tuple[Evidence, ...]:
    return tuple(
        Evidence(metric=metric, actual=actual, target=target, window_preset=window)
        for metric, actual, target, window in rows
    )


def _current_budget(entity: AdEntity, currency: str) -> Money:
    if entity.budget is None:
        return Money.zero(currency)
    if currency != entity.budget.amount.currency:
        raise McpValidationError("La divisa solicitada no coincide con la de la cuenta.")
    amount = Decimal(entity.budget.amount.minor_units) / _MINOR_UNITS_PER_MAJOR
    return Money.of(amount, currency)


def _pause_values(entity: AdEntity) -> tuple[str, str]:
    if entity.status not in {AdEntityStatus.ACTIVE, AdEntityStatus.LEARNING}:
        raise McpValidationError("La entidad no esta activa para proponer una pausa.")
    # Status diffs must contain statuses, never a monetary proxy that would
    # be misclassified as RESUME when translated to the broker operation.
    return entity.status.value, AdEntityStatus.PAUSED.value


def _status_diff(
    entity: AdEntity, *, allowed: frozenset[AdEntityStatus], target: str
) -> tuple[str, str]:
    if entity.status not in allowed:
        raise McpValidationError(
            f"La entidad no esta en un estado valido para esta accion "
            f"(actual={entity.status.value})."
        )
    return entity.status.value, target


def _currency_of(entity: AdEntity) -> str:
    return entity.budget.amount.currency if entity.budget is not None else "EUR"


def _current_bid_target(entity: AdEntity) -> Money | None:
    # `AdEntity.bid_target` es `accounts.domain.money.Money` (moneda propia
    # del contexto `accounts`): se traduce al `Money` de `proposals` antes
    # de entrar en el diff, igual que `_current_budget` hace con el
    # presupuesto -- dos VO con el mismo nombre, nunca el mismo tipo.
    if entity.bid_target is None:
        return None
    amount = Decimal(entity.bid_target.minor_units) / _MINOR_UNITS_PER_MAJOR
    return Money.of(amount, entity.bid_target.currency)


def _impact(before: Money, after: Money) -> Money:
    return (before - after) if before > after else (after - before)


def _to_dto(result: ProposeActionResult) -> ProposalWriteResult:
    return ProposalWriteResult(
        proposal_id=str(result.proposal_id),
        estado=result.state.value,
        diff_hash=result.diff_hash,
        expires_at=result.expires_at,
        classification=result.classification.value,
    )
