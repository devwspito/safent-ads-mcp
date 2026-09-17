"""`/api/v1/proposals*`, `/api/v1/executions*`, `/api/v1/kill-switch`
(contracts/rest-api.md §Propuestas, §Ejecucion): la aprobacion humana --
`SubmitApproval` es la MISMA pieza que usara Telegram (un solo camino de
decision). Vive en `composition/`, no en `execution.presentation`/
`proposals.presentation`: necesita `Container.build_execution_use_cases`
por peticion (abrir sesion, ejecutar, confirmar), y esos contextos no
pueden importar la raiz de composicion sin invertir el grafo (plan.md §4)
-- mismo motivo que `composition/mcp_write_adapter.py`.

Alcance reducido a proposito frente al contrato completo (Assumption
documentada, integration-engineer -- ver el informe de esta rama):
implementa listar/detalle/aprobar/rechazar/aprobar-en-lote propuestas,
deshacer ejecuciones (lote), el freno de emergencia (lectura + accion),
`PUT /rules/{id}`/`PUT /guardrails/{id}` y la puerta de autonomia
(`GET /rules/autonomy-gate`, `POST /rules/autonomy-gate/confirmations`).
Quedan fuera `PATCH /proposals/{id}` (editar valor), `PUT
/proposals/{id}/owner-context`, `POST /proposals/{id}/postpone`, `POST
/rules` (alta) y la edicion de `condition`/`window`/`action`/
`magnitude_pct`/`cooldown` de una regla existente (`RuleRepository` solo
expone `set_autonomy`, tasks.md §Notas 4: esos campos son del fichero
`rules.yaml`, no del propietario via panel): cada uno es una pieza de
negocio nueva o exige ampliar un puerto fuera del alcance de esta rama,
no wiring de lo ya construido.

`PUT /rules/{id}`/`PUT /guardrails/{id}`/autonomia viven aqui, no en
`rules.presentation`/`rules.infrastructure` (fuera de alcance de esta
rama): necesitan `Container.build_execution_use_cases` (el
`SqlGuardrailSetRepository` compuesto en Money para el veto
`SCOPE_CANNOT_RELAX`) y acceso directo a `platform_accounts`/
`autonomy_confirmations` (0019) que ningun puerto existente expone
todavia -- mismo criterio de ubicacion que el resto del archivo. El mapeo
puro (DTOs, invariante `Rule`, claves de la puerta) vive en
`rules.presentation.rest`/`proposals.presentation.rest`."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.infrastructure.sql_repositories import SqlAdEntityRepository
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.composition.container import Container, ExecutionUseCases
from safent_ads.execution.application.entity_lifecycle_actions import (
    DeleteEntityCommand,
    EntityActionDeniedError,
    EntityActionResult,
    PauseEntityCommand,
    ResumeEntityCommand,
    UnknownEntityError,
)
from safent_ads.execution.application.toggle_emergency_brake import (
    BrakeAlreadyEngagedError,
    EngageBrakeCommand,
    NoBrakeRegisteredError,
    ReleaseBrakeCommand,
)
from safent_ads.execution.application.undo_execution import (
    ExecutionAlreadyUndoneError,
    UndoExecutionCommand,
    UndoNotAllowedError,
    UndoOutcome,
)
from safent_ads.execution.domain.execution_attempt import ExecutionStatus
from safent_ads.execution.domain.guardrails import (
    BrakeMode,
    BrakeScope,
    BrakeScopeKind,
    GuardrailScope,
    GuardrailSet,
    ScopeKind,
)
from safent_ads.execution.infrastructure.errors import (
    MissingGuardrailSetError,
    UnknownEntityRefError,
)
from safent_ads.execution.infrastructure.kill_switch_view import read_kill_switch_view
from safent_ads.execution.infrastructure.sql_guardrail_sets import SqlGuardrailSetRepository
from safent_ads.iam.presentation.action_confirmation import require_action_confirmation
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import (
    AuthenticatedCaller,
    CallerDep,
    ensure_business_access,
    require_business_access,
)
from safent_ads.proposals.application.submit_approval import (
    ProposalApprovalDeniedError,
    SubmitApprovalCommand,
)
from safent_ads.proposals.domain.authorization import AuthorizationChannel
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.proposal import ProposalState
from safent_ads.proposals.infrastructure.value_codec import money_from_minor
from safent_ads.proposals.presentation.panel_read import build_proposal_read_router
from safent_ads.proposals.presentation.rest import (
    InvalidBatchApproveBodyError,
    batch_approve_envelope,
    parse_batch_approve_items,
)
from safent_ads.rules.domain.autonomy import AutonomyLevel
from safent_ads.rules.domain.guardrail import GuardrailPolicy
from safent_ads.rules.domain.stored_rule import StoredRule
from safent_ads.rules.infrastructure.sql_repositories import (
    SqlGuardrailRepository,
    SqlRuleRepository,
)
from safent_ads.rules.presentation.guardrail_serializers import (
    guardrail_policy_json,
    guardrail_policy_payload,
    parse_guardrail_policy,
)
from safent_ads.rules.presentation.rest import (
    account_gate_view,
    apply_autonomy_level,
    is_known_gate_key,
    stored_rule_to_json,
)
from safent_ads.shared.ids import BusinessId, EntityRef, EntityRefFormatError

_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]
# `require_business_access` (panel.presentation.deps): a diferencia de
# `ensure_business_access` (solo alcance del caller), tambien comprueba
# contra `SqlBusinessDirectory` que el negocio existe -- fail closed, 404
# nunca 403, para las rutas nuevas de esta rama que reciben `business_id`
# como query param directo (threat-model.md C-27).
_ScopedBusinessIdDep = Annotated[str, Depends(require_business_access)]

_NOT_FOUND = ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")
_ZERO_BUSINESS_ID = BusinessId.parse("00000000-0000-0000-0000-000000000000")

_DENIAL_STATUS: dict[str, int] = {
    "DIFF_CHANGED": 409,
    "PROPOSAL_EXPIRED": 409,
    "PROPOSAL_NOT_PENDING": 409,
    "BRAKE_ENGAGED": 409,
    "GUARDRAIL_BLOCKED": 422,
    "CHANNEL_TYPE_NOT_ENABLED": 422,
}
_MAX_BATCH_UNDO = 25
_PERCENT = 100.0

_FIND_ACCOUNT = text(
    "SELECT id, business_id, currency FROM platform_accounts WHERE account_ref = :account_ref"
)
_LIST_ACCOUNTS_FOR_BUSINESS = text(
    "SELECT id, platform, external_account_id, account_ref FROM platform_accounts "
    "WHERE business_id = :business_id ORDER BY platform, external_account_id"
)
_LIST_CONFIRMATIONS_FOR_ACCOUNT = text(
    "SELECT key, value, confirmed_at, confirmed_by FROM autonomy_confirmations "
    "WHERE platform_account_id = :platform_account_id"
)
_UPSERT_CONFIRMATION = text("""
    INSERT INTO autonomy_confirmations
        (business_id, platform_account_id, key, value, comment, confirmed_by, confirmed_at)
    VALUES (:business_id, :platform_account_id, :key, :value, :comment, :confirmed_by,
            :confirmed_at)
    ON CONFLICT ON CONSTRAINT autonomy_confirmations_unique DO UPDATE
        SET value = EXCLUDED.value, comment = EXCLUDED.comment,
            confirmed_by = EXCLUDED.confirmed_by, confirmed_at = EXCLUDED.confirmed_at
""")


def build_execution_router(container: Container) -> APIRouter:  # noqa: PLR0915 - raiz de router, cada handler es de 5-10 lineas
    router = APIRouter(prefix="/api/v1", tags=["execution"])

    router.include_router(build_proposal_read_router(container.session_factory))

    @router.post("/proposals/batch/approve", status_code=207)
    async def batch_approve_proposals(
        business_id: _ScopedBusinessIdDep,
        owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        try:
            items = parse_batch_approve_items(body)
        except InvalidBatchApproveBodyError as exc:
            raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc
        expected_business = BusinessId.parse(business_id)
        async with container.session_factory() as session:
            use_cases = container.build_execution_use_cases(session)
            results = [
                await _approve_batch_item(use_cases, item, owner.email, expected_business)
                for item in items
            ]
            await session.commit()
        return batch_approve_envelope(results)

    # `/proposals/batch/approve` esta registrada ANTES que
    # `/proposals/{proposal_id}/approve`: Starlette resuelve por orden de
    # registro (mismo criterio que `composition/app.py::_mount_panel_spa`),
    # y ambas plantillas tienen la misma forma de segmentos
    # (`proposals/<X>/approve`) -- sin este orden, un POST a la ruta de
    # lote se colaria por `{proposal_id}="batch"` y pediria `diff_hash` en
    # la raiz del cuerpo en vez de `items` (regresion detectada por
    # `test_batch_approve_is_207_with_per_item_outcomes`).
    @router.post("/proposals/{proposal_id}/approve")
    async def approve_proposal(
        proposal_id: str, owner: _OwnerDep, body: Annotated[dict[str, Any], Body(...)]
    ) -> dict[str, Any]:
        diff_hash = _require_str(body, "diff_hash")
        comment = body.get("comment")
        async with container.session_factory() as session:
            use_cases = container.build_execution_use_cases(session)
            await _require_visible_proposal(use_cases, proposal_id)
            try:
                result = await use_cases.submit_approval.execute(
                    SubmitApprovalCommand(
                        proposal_id=ProposalId.parse(proposal_id),
                        diff_hash=diff_hash,
                        approved_by=owner.email,
                        channel=AuthorizationChannel.PANEL,
                        comment=comment,
                    )
                )
            except ProposalApprovalDeniedError as exc:
                raise _denial_to_api_error(exc.reason.value, str(exc)) from exc
            await session.commit()
        return {
            "authorization_id": result.authorization_id,
            "execution_id": result.execution_id,
            "execution_scheduled_at": result.execution_scheduled_at.isoformat(),
            "undo_deadline": None,
            "grace_seconds": result.grace_seconds,
        }

    @router.post("/proposals/{proposal_id}/reject")
    async def reject_proposal(
        proposal_id: str, owner: _OwnerDep, body: Annotated[dict[str, Any], Body(...)]
    ) -> dict[str, Any]:
        del owner
        diff_hash = _require_str(body, "diff_hash")
        comment = body.get("comment")
        async with container.session_factory() as session:
            use_cases = container.build_execution_use_cases(session)
            proposal = await _require_visible_proposal(use_cases, proposal_id)
            if diff_hash != proposal.diff.diff_hash:
                raise _denial_to_api_error("DIFF_CHANGED", "diff_hash no coincide")
            if proposal.state is not ProposalState.PENDING:
                raise _denial_to_api_error(
                    "PROPOSAL_NOT_PENDING", f"estado actual: {proposal.state.value}"
                )
            proposal.reject(container.clock.now(), comment)
            await use_cases.proposals.save(proposal)
            await session.commit()
        return {"state": proposal.state.value}

    @router.post("/executions/undo")
    async def undo_executions(
        owner: _OwnerDep, body: Annotated[dict[str, Any], Body(...)]
    ) -> dict[str, Any]:
        execution_ids = body.get("execution_ids") or []
        is_valid_batch = (
            isinstance(execution_ids, list) and 0 < len(execution_ids) <= _MAX_BATCH_UNDO
        )
        if not is_valid_batch:
            raise ApiError(
                status_code=422,
                code="VALIDATION_ERROR",
                message=f"execution_ids: 1-{_MAX_BATCH_UNDO} elementos",
            )
        async with container.session_factory() as session:
            use_cases = container.build_execution_use_cases(session)
            results = [
                await _undo_one(session, use_cases, execution_id, owner.email)
                for execution_id in execution_ids
            ]
            await session.commit()
        return {"results": results}

    @router.post("/entities/{entity_ref:path}/pause")
    async def pause_entity(entity_ref: str, owner: _OwnerDep, caller: CallerDep) -> dict[str, Any]:
        ref = _parse_entity_ref(entity_ref)
        async with container.session_factory() as session:
            await _require_entity_access(session, ref, caller)
            use_cases = container.build_execution_use_cases(session)
            result = await _run_entity_action(
                use_cases.pause_entity.execute(
                    PauseEntityCommand(entity_ref=ref, owner_email=owner.email)
                )
            )
            await session.commit()
        return _pause_resume_body(result)

    @router.post("/entities/{entity_ref:path}/resume")
    async def resume_entity(entity_ref: str, owner: _OwnerDep, caller: CallerDep) -> dict[str, Any]:
        ref = _parse_entity_ref(entity_ref)
        async with container.session_factory() as session:
            await _require_entity_access(session, ref, caller)
            use_cases = container.build_execution_use_cases(session)
            result = await _run_entity_action(
                use_cases.resume_entity.execute(
                    ResumeEntityCommand(entity_ref=ref, owner_email=owner.email)
                )
            )
            await session.commit()
        return _pause_resume_body(result)

    @router.post("/entities/{entity_ref:path}/delete")
    async def delete_entity(entity_ref: str, owner: _OwnerDep, caller: CallerDep) -> dict[str, Any]:
        ref = _parse_entity_ref(entity_ref)
        async with container.session_factory() as session:
            await _require_entity_access(session, ref, caller)
            use_cases = container.build_execution_use_cases(session)
            result = await _run_entity_action(
                use_cases.delete_entity.execute(
                    DeleteEntityCommand(entity_ref=ref, owner_email=owner.email)
                )
            )
            await session.commit()
        return _delete_body(result)

    @router.get("/kill-switch")
    async def get_kill_switch(business_id: _ScopedBusinessIdDep) -> dict[str, Any]:
        async with container.session_factory() as session:
            return await read_kill_switch_view(session, business_id)

    @router.post("/kill-switch")
    async def post_kill_switch(
        owner: _OwnerDep,
        business_id: _ScopedBusinessIdDep,
        caller: CallerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        scope = _parse_brake_scope(body)
        engaged = body.get("engaged")
        if not isinstance(engaged, bool):
            raise ApiError(
                status_code=422, code="VALIDATION_ERROR", message="engaged debe ser booleano."
            )
        reason = str(body.get("reason") or "")
        try:
            mode = BrakeMode(str(body.get("mode", "all")).lower())
        except ValueError as exc:
            raise ApiError(
                status_code=422, code="VALIDATION_ERROR", message="Modo de freno invalido."
            ) from exc
        actor = f"owner:{owner.email}"
        async with container.session_factory() as session:
            if scope.kind is BrakeScopeKind.GLOBAL:
                # A caller restricted to one business must not change the global
                # physical brake. Only the existing unrestricted owner may do so.
                if caller.allowed_business_ids is not None:
                    raise _NOT_FOUND
            elif scope.kind is BrakeScopeKind.BUSINESS:
                if scope.ref != business_id:
                    raise _NOT_FOUND
            else:
                account = await _find_account(session, scope.ref or "")
                if account is None or str(account["business_id"]) != business_id:
                    raise _NOT_FOUND
            use_cases = container.build_execution_use_cases(session, brake_actor=actor)
            await _toggle_brake(
                use_cases,
                scope,
                engaged=engaged,
                reason=reason,
                actor=actor,
                mode=mode,
                business_id=BusinessId.parse(business_id),
            )
            view = await read_kill_switch_view(session, business_id)
            await session.commit()
        return view

    @router.put("/rules/{rule_code}")
    async def put_rule(
        rule_code: str,
        business_id: _ScopedBusinessIdDep,
        owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        autonomy_level = _parse_autonomy_level(body)
        async with container.session_factory() as session:
            rules = SqlRuleRepository(session)
            stored = await rules.get_by_code(rule_code)
            if stored is None:
                raise _NOT_FOUND
            updated_rule = apply_autonomy_level(stored.rule, autonomy_level)
            enabled = bool(body.get("enabled", stored.is_enabled))
            if autonomy_level is AutonomyLevel.AUTO:
                await _require_autonomy_gate_ready(session, business_id)
            await rules.set_autonomy(code=rule_code, level=autonomy_level, enabled=enabled)
            await _record_decision(
                session,
                business_id=BusinessId.parse(business_id),
                actor_email=owner.email,
                kind=DecisionKind.RULE_CHANGE,
                payload={
                    "rule_code": rule_code,
                    "autonomy_level": autonomy_level.value,
                    "enabled": enabled,
                },
            )
            await session.commit()
        return stored_rule_to_json(StoredRule(rule=updated_rule, is_enabled=enabled))

    @router.put("/guardrails/{account_ref}")
    async def put_guardrail(
        account_ref: str,
        business_id: _ScopedBusinessIdDep,
        owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        policy = parse_guardrail_policy(body)
        async with container.session_factory() as session:
            account = await _find_account(session, account_ref)
            if account is None or str(account["business_id"]) != business_id:
                raise _NOT_FOUND
            currency = str(account["currency"])
            await _require_no_scope_relaxation(session, business_id, account_ref, policy, currency)
            await SqlGuardrailRepository(session).save_for_account(
                account_ref=account_ref, policy=policy, currency=currency
            )
            await _record_decision(
                session,
                business_id=BusinessId.parse(business_id),
                actor_email=owner.email,
                kind=DecisionKind.GUARDRAIL_CHANGE,
                payload={"account_ref": account_ref, **guardrail_policy_payload(policy)},
            )
            await session.commit()
        return guardrail_policy_json(account_ref, policy, currency)

    @router.get("/rules/autonomy-gate")
    async def get_autonomy_gate(business_id: _ScopedBusinessIdDep) -> dict[str, Any]:
        async with container.session_factory() as session:
            return await _autonomy_gate_view(session, business_id)

    @router.post("/rules/autonomy-gate/confirmations")
    async def post_autonomy_gate_confirmation(
        request: Request,
        owner: _OwnerDep,
        caller: CallerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        platform_account_id = _require_str(body, "platform_account_id")
        key = _require_str(body, "key")
        value = _require_str(body, "value")
        if not is_known_gate_key(key):
            raise ApiError(
                status_code=422, code="VALIDATION_ERROR", message=f"key desconocida: {key}"
            )
        async with container.session_factory() as session:
            account = await _find_account(session, platform_account_id)
            if account is None:
                raise _NOT_FOUND
            ensure_business_access(str(account["business_id"]), caller)
            action_hash = _reauth_action_hash(
                business_id=str(account["business_id"]),
                platform_account_id=platform_account_id,
                key=key,
                value=value,
            )
            await _require_action_confirmation(
                request, session, container, owner, action_hash=action_hash
            )
            confirmed_at = container.clock.now()
            await session.execute(
                _UPSERT_CONFIRMATION,
                {
                    "business_id": account["business_id"],
                    "platform_account_id": account["id"],
                    "key": key,
                    "value": value,
                    "comment": body.get("comment"),
                    "confirmed_by": owner.owner_id,
                    "confirmed_at": confirmed_at,
                },
            )
            await _record_decision(
                session,
                business_id=BusinessId(account["business_id"]),
                actor_email=owner.email,
                kind=DecisionKind.RULE_CHANGE,
                payload={
                    "event": "AutonomyConfirmationRecorded",
                    "platform_account_id": platform_account_id,
                    "key": key,
                },
            )
            await session.commit()
        return {
            "platform_account_id": platform_account_id,
            "key": key,
            "value": value,
            "confirmed_at": confirmed_at.isoformat(),
        }

    return router


async def _require_visible_proposal(use_cases: ExecutionUseCases, proposal_id: str) -> Any:
    proposal = await use_cases.proposals.get(ProposalId.parse(proposal_id))
    if proposal is None:
        raise _NOT_FOUND
    return proposal


async def _toggle_brake(
    use_cases: ExecutionUseCases,
    scope: BrakeScope,
    *,
    engaged: bool,
    reason: str,
    actor: str,
    mode: BrakeMode,
    business_id: BusinessId | None = None,
) -> Any:
    toggle = use_cases.toggle_emergency_brake
    business_id = business_id or _business_id_for_scope(scope)
    try:
        if engaged:
            return await toggle.engage(
                EngageBrakeCommand(
                    business_id=business_id,
                    scope=scope,
                    mode=mode,
                    reason=reason,
                    engaged_by=actor,
                )
            )
        return await toggle.release(
            ReleaseBrakeCommand(business_id=business_id, scope=scope, released_by=actor)
        )
    except BrakeAlreadyEngagedError as exc:
        raise ApiError(status_code=409, code="BRAKE_ALREADY_ENGAGED", message=str(exc)) from exc
    except NoBrakeRegisteredError as exc:
        raise ApiError(status_code=409, code="NO_BRAKE_REGISTERED", message=str(exc)) from exc


async def _undo_one(
    session: AsyncSession, use_cases: ExecutionUseCases, execution_id: str, initiated_by: str
) -> dict[str, Any]:
    proposal_id = await _proposal_id_for_execution(session, execution_id)
    if proposal_id is None:
        return {"execution_id": execution_id, "ok": False, "error_code": "NOT_FOUND"}
    try:
        result = await use_cases.undo_execution.execute(
            UndoExecutionCommand(
                proposal_id=ProposalId.parse(proposal_id), initiated_by=initiated_by
            )
        )
    except ExecutionAlreadyUndoneError:
        return {"execution_id": execution_id, "ok": False, "error_code": "EXECUTION_ALREADY_UNDONE"}
    except UndoNotAllowedError as exc:
        return {"execution_id": execution_id, "ok": False, "error_code": str(exc)}
    is_cancelled = result.outcome is UndoOutcome.CANCELLED_SCHEDULED
    return {
        "execution_id": execution_id,
        "ok": True,
        "undo_kind": "cancelled" if is_cancelled else "compensated",
        "compensating_proposal_id": (
            str(result.compensating_proposal_id)
            if result.compensating_proposal_id is not None
            else None
        ),
    }


async def _proposal_id_for_execution(session: AsyncSession, execution_id: str) -> str | None:
    result = await session.execute(
        text("SELECT proposal_id FROM executions WHERE id = :id"), {"id": execution_id}
    )
    row = result.one_or_none()
    return None if row is None else str(row.proposal_id)


# ---------------------------------------------------------------------------
# `POST /entities/{entity_ref}/pause|resume|delete`
# ---------------------------------------------------------------------------


def _parse_entity_ref(raw: str) -> EntityRef:
    try:
        return EntityRef.parse(raw)
    except EntityRefFormatError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="entity_ref invalido"
        ) from exc


async def _require_entity_access(
    session: AsyncSession, entity_ref: EntityRef, caller: AuthenticatedCaller
) -> None:
    """Mismo criterio que `panel.presentation.rest`: resuelve el negocio
    dueño de la entidad contra la base y comprueba el alcance del caller
    ANTES de mutar nada -- 404, nunca 403, para no filtrar existencia entre
    negocios (threat-model.md C-27)."""
    entity = await SqlAdEntityRepository(session).get_by_ref(entity_ref)
    if entity is None:
        raise _NOT_FOUND
    ensure_business_access(str(entity.business_id), caller)


async def _run_entity_action(
    execution: Awaitable[EntityActionResult],
) -> EntityActionResult:
    try:
        return await execution
    except UnknownEntityError as exc:
        raise _NOT_FOUND from exc
    except EntityActionDeniedError as exc:
        raise ApiError(status_code=409, code=exc.code.value, message=str(exc)) from exc


def _ensure_executed(result: EntityActionResult) -> EntityActionResult:
    """El caso de uso nunca lanza por un fallo de plataforma (`Execution
    Chokepoint.run_once` siempre devuelve un `ExecutionStatus`, plan.md §6):
    el desenlace se inspecciona AQUI, despues de confirmar la transaccion
    que dejo el intento auditado en `executions` -- rechazar antes del
    commit borraria ese registro (BLOCKED_BRAKE/FAILED/UNKNOWN incluidos)."""
    if result.outcome is ExecutionStatus.EXECUTED:
        return result
    if result.outcome is ExecutionStatus.BLOCKED_BRAKE:
        raise ApiError(
            status_code=409,
            code="BRAKE_ENGAGED",
            message="El freno de emergencia detuvo la escritura.",
        )
    raise ApiError(
        status_code=502,
        code="PLATFORM_WRITE_FAILED",
        message="No se pudo aplicar el cambio en la plataforma.",
    )


def _pause_resume_body(result: EntityActionResult) -> dict[str, Any]:
    executed = _ensure_executed(result)
    return {
        "execution_id": str(executed.execution_id),
        "undo_deadline": (
            executed.undo_deadline.isoformat() if executed.undo_deadline is not None else None
        ),
    }


def _delete_body(result: EntityActionResult) -> dict[str, Any]:
    return {"execution_id": str(_ensure_executed(result).execution_id)}


def _require_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=f"{key} requerido")
    return value


def _parse_autonomy_level(body: dict[str, Any]) -> AutonomyLevel:
    raw = _require_str(body, "autonomy_level")
    try:
        # El panel envia el enum en mayusculas (`autonomyLevelSchema`);
        # el dominio lo guarda en minusculas.
        return AutonomyLevel(raw.lower())
    except ValueError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message=f"autonomy_level invalido: {raw}"
        ) from exc


def _denial_to_api_error(code: str, message: str) -> ApiError:
    status_code = _DENIAL_STATUS.get(code, 409)
    return ApiError(status_code=status_code, code=code, message=message)


def _parse_brake_scope(body: dict[str, Any]) -> BrakeScope:
    try:
        kind = BrakeScopeKind(str(body.get("scope_kind", "global")).lower())
    except ValueError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="Ambito de freno invalido."
        ) from exc
    scope_id = body.get("scope_id")
    if (kind is BrakeScopeKind.GLOBAL and scope_id is not None) or (
        kind is not BrakeScopeKind.GLOBAL and (not isinstance(scope_id, str) or not scope_id)
    ):
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message="Referencia de freno invalida."
        )
    return BrakeScope(kind=kind, ref=scope_id)


def _business_id_for_scope(scope: BrakeScope) -> BusinessId:
    if scope.kind is BrakeScopeKind.BUSINESS and scope.ref:
        return BusinessId.parse(scope.ref)
    # Freno global/de cuenta: `Engage/ReleaseBrakeCommand.business_id` solo
    # alimenta el evento de `decision_log` (`EmergencyBrakeEngaged.
    # business_id`), nunca resuelve el ambito -- eso lo decide `scope`.
    # Sin negocio real que asociar a un freno global, un id nulo
    # documentado es mas honesto que inventar uno (Assumption).
    return _ZERO_BUSINESS_ID


# ---------------------------------------------------------------------------
# `POST /proposals/batch/approve`
# ---------------------------------------------------------------------------


async def _approve_batch_item(
    use_cases: ExecutionUseCases, item: Any, approved_by: str, expected_business: BusinessId
) -> dict[str, Any]:
    proposal = await use_cases.proposals.get(ProposalId.parse(item.proposal_id))
    if proposal is None or proposal.business_id != expected_business:
        return {"proposal_id": item.proposal_id, "ok": False, "error_code": "NOT_FOUND"}
    try:
        result = await use_cases.submit_approval.execute(
            SubmitApprovalCommand(
                proposal_id=ProposalId.parse(item.proposal_id),
                diff_hash=item.diff_hash,
                approved_by=approved_by,
                channel=AuthorizationChannel.PANEL,
            )
        )
    except ProposalApprovalDeniedError as exc:
        return {
            "proposal_id": item.proposal_id,
            "ok": False,
            "error_code": exc.reason.value,
            "message": str(exc),
        }
    return {
        "proposal_id": item.proposal_id,
        "ok": True,
        "execution_id": result.execution_id,
        "execution_scheduled_at": result.execution_scheduled_at.isoformat(),
        "grace_seconds": result.grace_seconds,
    }


# ---------------------------------------------------------------------------
# `PUT /rules/{id}` / `PUT /guardrails/{id}` / puerta de autonomia
# ---------------------------------------------------------------------------


async def _find_account(session: AsyncSession, account_ref: str) -> RowMapping | None:
    platform, _, external_account_id = account_ref.partition(":")
    result = await session.execute(
        _FIND_ACCOUNT,
        {
            "platform": platform,
            "external_account_id": external_account_id,
            "account_ref": account_ref,
        },
    )
    return result.mappings().one_or_none()


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


async def _autonomy_gate_view(session: AsyncSession, business_id: str) -> dict[str, Any]:
    accounts = (
        (await session.execute(_LIST_ACCOUNTS_FOR_BUSINESS, {"business_id": business_id}))
        .mappings()
        .all()
    )
    account_views = [await _one_account_gate_view(session, row) for row in accounts]
    ready = all(view["ready"] for view in account_views)
    return {"ready": ready, "accounts": account_views}


async def _one_account_gate_view(session: AsyncSession, row: RowMapping) -> dict[str, Any]:
    confirmed_rows = (
        (await session.execute(_LIST_CONFIRMATIONS_FOR_ACCOUNT, {"platform_account_id": row["id"]}))
        .mappings()
        .all()
    )
    confirmed = [
        {
            "key": confirmation["key"],
            "value": confirmation["value"],
            "confirmed_at": confirmation["confirmed_at"].isoformat(),
            "confirmed_by": str(confirmation["confirmed_by"]),
        }
        for confirmation in confirmed_rows
    ]
    platform_account_id = row["account_ref"]
    return account_gate_view(
        platform_account_id=platform_account_id,
        label=f"{row['platform']} · {row['external_account_id']}",
        confirmed=confirmed,
    )


async def _require_autonomy_gate_ready(session: AsyncSession, business_id: str) -> None:
    view = await _autonomy_gate_view(session, business_id)
    if view["ready"]:
        return
    missing = [item for account in view["accounts"] for item in account["missing"]]
    raise ApiError(
        status_code=409,
        code="AUTONOMY_GATE_OPEN",
        message="Puerta de autonomia abierta: faltan confirmaciones del propietario.",
        details={"missing": missing},
    )


def _reauth_action_hash(*, business_id: str, platform_account_id: str, key: str, value: str) -> str:
    """Semantic gate intent, combined with the exact HTTP request in the proof."""
    payload = f"{business_id}|{platform_account_id}|{key}|{value}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _require_action_confirmation(
    request: Request,
    session: AsyncSession,
    container: Container,
    owner: AuthenticatedOwner,
    *,
    action_hash: str,
) -> None:
    """One shared, durable owner-confirmation boundary for sensitive actions."""
    await require_action_confirmation(
        request,
        session,
        owner,
        action_hash=action_hash,
        clock=container.clock,
    )


async def _require_no_scope_relaxation(
    session: AsyncSession,
    business_id: str,
    account_ref: str,
    policy: GuardrailPolicy,
    currency: str,
) -> None:
    """422 `SCOPE_CANNOT_RELAX` si el ambito de cuenta amplia algun limite
    del ambito de negocio (`GuardrailSet.effective_with`, execution.domain.
    guardrails): mismo servicio de dominio que compone el chokepoint, para
    no reescribir la regla dos veces."""
    try:
        general = await SqlGuardrailSetRepository(session).get_effective(
            GuardrailScope(kind=ScopeKind.BUSINESS, ref=business_id)
        )
    except (MissingGuardrailSetError, UnknownEntityRefError):
        return
    candidate = GuardrailSet(
        scope=GuardrailScope(kind=ScopeKind.PLATFORM_ACCOUNT, ref=account_ref),
        daily_cap=money_from_minor(policy.daily_cap_minor, currency),
        monthly_cap=money_from_minor(policy.monthly_cap_minor, currency),
        floor=money_from_minor(policy.floor_minor, currency),
        ceiling=money_from_minor(policy.ceiling_minor, currency),
        max_step_pct=policy.max_step_pct / _PERCENT,
        max_changes_per_entity_day=policy.max_changes_per_day,
    )
    if not candidate.is_at_least_as_restrictive_as(general):
        raise ApiError(
            status_code=422,
            code="SCOPE_CANNOT_RELAX",
            message=f"el ambito {account_ref} relaja limites del negocio que lo contiene",
        )
