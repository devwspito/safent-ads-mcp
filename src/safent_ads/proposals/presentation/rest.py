"""Borde HTTP de `proposals` (contracts/rest-api.md `POST
/proposals/batch/approve`, `PUT .../owner-context`, `POST .../postpone`,
`PATCH /proposals/{id}`).

`parse_batch_approve_items`/`batch_approve_envelope` son funciones puras de
parseo/ensamblado que `composition/execution_rest.py` llama -- viven aqui,
no en `composition/`, porque no necesitan `Container` (mismo criterio que
`rules/presentation/rest.py`). `POST /proposals/batch/approve` **nunca** es
atomico (contracts/rest-api.md §Propuestas: "el lote nunca es atomico:
siempre 207, cada resultado con su motivo"): `batch_approve_envelope` solo
agrega resultados ya resueltos uno a uno, nunca decide si el lote "tuvo
exito".

`build_proposal_admin_router` SI necesita tocar la base de datos
(`owner_context`/`postpone`/editar `valor_propuesto`), pero no el resto de
`Container.build_execution_use_cases` (guardarrailes, freno, firma de
autorizaciones): sobre `SqlProposalRepository(session)` a secas le basta,
mismo criterio de "adaptador request-scoped sobre `session_factory`" que
`composition/economics_rest.py::build_economics_read_router` -- por eso
vive aqui y no en `composition/execution_rest.py` (que esta rama tiene
instruccion explicita de no hacer crecer)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.execution.infrastructure.sql_unit_of_work import SqlUnitOfWork
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, AuthenticatedOwner
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import (
    AuthenticatedCaller,
    CallerDep,
    ensure_business_access,
)
from safent_ads.proposals.domain.campaign_creation import (
    CampaignCreationError,
    creation_budget,
    google_channel_from_creation_plan,
)
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import (
    MAX_OWNER_CONTEXT_LENGTH,
    Proposal,
    ProposalInvariantError,
)
from safent_ads.proposals.infrastructure.sql_proposal_repository import SqlProposalRepository
from safent_ads.shared.clock import Clock

__all__ = [
    "MAX_BATCH_APPROVE_ITEMS",
    "BatchApproveItem",
    "InvalidBatchApproveBodyError",
    "batch_approve_envelope",
    "build_proposal_admin_router",
    "parse_batch_approve_items",
]

MAX_BATCH_APPROVE_ITEMS = 25
_OwnerDep = Annotated[AuthenticatedOwner, CURRENT_OWNER]
_NOT_FOUND = ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")


class InvalidBatchApproveBodyError(ValueError):
    """El cuerpo de `POST /proposals/batch/approve` no respeta la forma del
    contrato (`items`: 1-25 `{proposal_id, diff_hash}`)."""


@dataclass(frozen=True, slots=True)
class BatchApproveItem:
    proposal_id: str
    diff_hash: str


def parse_batch_approve_items(body: dict[str, Any]) -> tuple[BatchApproveItem, ...]:
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not (0 < len(raw_items) <= MAX_BATCH_APPROVE_ITEMS):
        raise InvalidBatchApproveBodyError(f"items: 1-{MAX_BATCH_APPROVE_ITEMS} elementos")
    return tuple(_parse_item(raw) for raw in raw_items)


def _parse_item(raw: Any) -> BatchApproveItem:
    if not isinstance(raw, dict):
        raise InvalidBatchApproveBodyError("cada item de 'items' debe ser un objeto")
    proposal_id = raw.get("proposal_id")
    diff_hash = raw.get("diff_hash")
    if not isinstance(proposal_id, str) or not proposal_id:
        raise InvalidBatchApproveBodyError("item.proposal_id requerido")
    if not isinstance(diff_hash, str) or not diff_hash:
        raise InvalidBatchApproveBodyError("item.diff_hash requerido")
    return BatchApproveItem(proposal_id=proposal_id, diff_hash=diff_hash)


def batch_approve_envelope(results: list[dict[str, Any]]) -> dict[str, Any]:
    approved = [result for result in results if result["ok"]]
    grace_seconds = approved[0]["grace_seconds"] if approved else 0
    return {
        "approved_count": len(approved),
        "failed_count": len(results) - len(approved),
        "grace_seconds": grace_seconds,
        "execution_ids": [result["execution_id"] for result in approved],
        "results": results,
    }


# ---------------------------------------------------------------------------
# `PUT /proposals/{id}/owner-context`, `POST /proposals/{id}/postpone`,
# `PATCH /proposals/{id}`
# ---------------------------------------------------------------------------


def build_proposal_admin_router(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
        {GoogleAdvertisingChannelType.SEARCH}
    ),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["proposals"])

    @router.put("/proposals/{proposal_id}/owner-context")
    async def put_owner_context(
        proposal_id: str,
        caller: CallerDep,
        _owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        text_value = _require_owner_context_text(body)
        async with session_factory() as session:
            proposals = SqlProposalRepository(session)
            proposal = await _require_visible_proposal(proposals, proposal_id, caller)
            try:
                proposal.set_owner_context(text_value)
            except ProposalInvariantError as exc:
                raise ApiError(status_code=422, code="VALIDATION_ERROR", message=str(exc)) from exc
            await proposals.save(proposal)
            await session.commit()
        return {}

    @router.post("/proposals/{proposal_id}/postpone")
    async def post_postpone(
        proposal_id: str,
        caller: CallerDep,
        _owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        until = _require_datetime_field(body, "until")
        async with session_factory() as session:
            proposals = SqlProposalRepository(session)
            proposal = await _require_visible_proposal(proposals, proposal_id, caller)
            try:
                proposal.postpone(until, clock.now())
            except ProposalInvariantError as exc:
                raise ApiError(
                    status_code=409, code="PROPOSAL_NOT_PENDING", message=str(exc)
                ) from exc
            await proposals.save(proposal)
            await session.commit()
        return {}

    @router.patch("/proposals/{proposal_id}")
    async def patch_proposal(
        proposal_id: str,
        caller: CallerDep,
        _owner: _OwnerDep,
        body: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        async with session_factory() as session:
            proposals = SqlProposalRepository(session)
            proposal = await _require_visible_proposal(proposals, proposal_id, caller)
            await SqlUnitOfWork(session).lock_account(proposal.diff.entity_ref)
            # Serialize edits with reservation admission, then reload the hash.
            proposal = await _require_visible_proposal(proposals, proposal_id, caller)
            inflight = (
                await session.execute(
                    text("""SELECT EXISTS (
                SELECT 1 FROM executions e LEFT JOIN execution_reservations r ON r.execution_id=e.id
                 WHERE e.proposal_id=:id AND e.business_id=:business
                   AND (e.outcome IN ('RUNNING','UNKNOWN') OR r.state='ACTIVE'))"""),
                    {"id": proposal_id, "business": str(proposal.business_id)},
                )
            ).scalar_one()
            if inflight:
                raise ApiError(
                    status_code=409,
                    code="EXECUTION_UNRESOLVED",
                    message="La ejecución ya tiene una reserva o un resultado sin resolver.",
                )
            new_value = _edited_value(proposal, body, enabled_google_channels)
            if new_value == proposal.diff.after:
                return {"diff_hash": proposal.diff.diff_hash}
            try:
                new_diff = proposal.edit_proposed_value(new_value, clock.now())
            except ProposalInvariantError as exc:
                raise ApiError(
                    status_code=409, code="PROPOSAL_NOT_EDITABLE", message=str(exc)
                ) from exc
            await proposals.save(proposal)
            await session.commit()
        return {"diff_hash": new_diff.diff_hash}

    return router


def _edited_value(
    proposal: Proposal,
    body: dict[str, Any],
    enabled_google_channels: frozenset[GoogleAdvertisingChannelType],
) -> object:
    if proposal.diff.parameter.startswith(("new_ad_set:", "new_ad:")):
        raise ApiError(
            status_code=422,
            code="CHILD_PLAN_EDIT_UNSUPPORTED",
            message="Retira esta propuesta y presenta un nuevo plan completo. "
            "No se admite edición numérica de un plan de creación.",
        )
    if not proposal.diff.parameter.startswith("new_campaign:"):
        return _coerce_proposed_value(proposal.diff.after, _require_number(body, "valor_propuesto"))
    if body.get("diff_hash") != proposal.diff.diff_hash:
        raise ApiError(status_code=409, code="DIFF_CHANGED", message="La propuesta ha cambiado.")
    if set(body) != {"diff_hash", "creation_plan"} or not isinstance(proposal.diff.after, dict):
        raise ApiError(
            status_code=422, code="CAMPAIGN_PLAN_INVALID", message="creation_plan requerido."
        )
    # T035 security re-check (CWE-284): the channel gate covered
    # `propose_campaign`/`propose_campaign_package` but not this edit --
    # an owner could propose SEARCH and PATCH `creation_plan` to a channel
    # this installation never enabled. Fail-closed, before the plan is
    # even merged into `new_value`, same code the MCP boundary already uses.
    edited_channel = google_channel_from_creation_plan(body["creation_plan"])
    if edited_channel is not None and edited_channel not in enabled_google_channels:
        raise ApiError(
            status_code=422,
            code="CHANNEL_TYPE_NOT_ENABLED",
            message=f"canal no habilitado en esta instalacion: {edited_channel.value}",
        )
    new_value = {**proposal.diff.after, "creation_plan": body["creation_plan"]}
    # Validate native fields before copying the explicitly edited budget into
    # the informational brief. No inferred/default targeting or bidding fields.
    try:
        budget = creation_budget({"creation_plan": body["creation_plan"]}, proposal.diff.entity_ref)
        if "daily_budget_amount" in new_value:
            new_value.update(
                daily_budget_amount=str(budget.amount), daily_budget_currency=budget.currency
            )
        creation_budget(new_value, proposal.diff.entity_ref)
    except (CampaignCreationError, TypeError) as exc:
        raise ApiError(
            status_code=422,
            code="CAMPAIGN_PLAN_INVALID",
            message=str(exc)
            if isinstance(exc, CampaignCreationError)
            else "campaign_creation_plan_invalid",
        ) from exc
    return new_value


async def _require_visible_proposal(
    proposals: SqlProposalRepository, proposal_id: str, caller: AuthenticatedCaller
) -> Proposal:
    proposal = await proposals.get(ProposalId.parse(proposal_id))
    if proposal is None:
        raise _NOT_FOUND
    ensure_business_access(str(proposal.business_id), caller)
    return proposal


def _require_owner_context_text(body: dict[str, Any]) -> str:
    value = body.get("text")
    if not isinstance(value, str):
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message="text requerido")
    if len(value) > MAX_OWNER_CONTEXT_LENGTH:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"text supera el maximo de {MAX_OWNER_CONTEXT_LENGTH} caracteres",
        )
    return value


def _require_datetime_field(body: dict[str, Any], key: str) -> datetime:
    raw = body.get(key)
    if not isinstance(raw, str) or not raw:
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=f"{key} requerido")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message=f"{key} debe ser ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        raise ApiError(
            status_code=422, code="VALIDATION_ERROR", message=f"{key} debe incluir zona horaria"
        )
    return parsed


def _require_number(body: dict[str, Any], key: str) -> float:
    """CWE-1287 (security-review-f4.md item 5): `json.loads` acepta los
    literales `NaN`/`Infinity`/`-Infinity`, y un numero negativo no es un
    `valor_propuesto` valido para ningun parametro de este contrato -- sin
    esta comprobacion, `Money.of`/`GuardrailEvaluator._clamp` reciben un
    valor que no saben evaluar (`InvalidOperation`/500) en vez de que el
    borde lo rechace con un 422 claro."""
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ApiError(status_code=422, code="VALIDATION_ERROR", message=f"{key} requerido")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"{key} debe ser un numero finito y no negativo",
        )
    return number


def _coerce_proposed_value(current_after: object, raw_value: float) -> object:
    """`PATCH /proposals/{id}` solo manda `valor_propuesto` en crudo
    (contracts/rest-api.md): si el parametro vivo es `Money` (presupuesto,
    puja), se envuelve preservando la divisa vigente -- el cliente nunca
    manda divisa, y cambiarla no es lo que este endpoint hace."""
    if isinstance(current_after, Money):
        return Money.of(raw_value, current_after.currency)
    return raw_value
