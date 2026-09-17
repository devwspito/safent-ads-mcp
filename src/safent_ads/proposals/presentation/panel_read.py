"""Read the owner inbox from persisted proposals, not from the smaller MCP summary.

Missing analytics remain null. Reading a proposal never grants authority to execute it.
The domain repository verifies the persisted diff hash before anything is shown.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.iam.presentation.errors import ApiError
from safent_ads.panel.presentation.deps import require_business_access
from safent_ads.proposals.domain.campaign_creation import CampaignCreationError, creation_budget
from safent_ads.proposals.domain.classification import Classification
from safent_ads.proposals.domain.identifiers import ProposalId
from safent_ads.proposals.domain.money import Money
from safent_ads.proposals.domain.proposal import Proposal, ProposalState
from safent_ads.proposals.infrastructure.sql_proposal_repository import (
    ProposalLens,
    SqlProposalRepository,
)
from safent_ads.proposals.presentation.rest import MAX_BATCH_APPROVE_ITEMS
from safent_ads.shared.http_cache import json_response_with_etag
from safent_ads.shared.ids import BusinessId

BusinessScope = Annotated[str, Depends(require_business_access)]
Lens = Literal["urgency", "calendar_event"]

# Bj-5: mismo prefijo que `composition/mcp_write_adapter.py::
# _NATIVE_WRITE_PARAMETER_TEMPLATE` ("native:{platform}:{operation}") --
# duplicado a proposito, presentacion no importa de composition (mismo
# criterio de aislamiento de capas que `meta_graph_path.py`). `Proposal` no
# persiste `ProposalKind`, asi que el prefijo del parametro es la unica
# senal que le queda al panel para distinguir una escritura nativa.
_NATIVE_WRITE_PARAMETER_PREFIX = "native:"


def display_value(value: object) -> str | float | int:
    if isinstance(value, Money):
        # Display only; edits and approvals still use the original domain payload/hash.
        return float(value.amount)
    if isinstance(value, str | int | float) and not isinstance(value, bool):
        return value
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def display_native_write_payload(value: object) -> str:
    """Bj-5: el payload libre de `propose_native_write` se enseña como un
    bloque JSON indentado, no aplastado en una linea -- `classification.py`
    ya dice que "el dueño la lee tal cual en el panel"; con `display_value`
    (una linea, sin `indent`) no se podia leer de verdad."""
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True, indent=2)


def _proposed_by_view(proposed_by: str | None) -> dict[str, Any] | None:
    """contracts/panel.md §2.5: `{kind, label} | null`. `label` queda sin
    resolver aqui a proposito (data-model.md §4 solo guarda `person:<uuid>`,
    nunca un nombre) -- el panel ya sabe mostrar «Propuesto por alguien del
    equipo» cuando `label` falta, nunca un uuid crudo."""
    if proposed_by is None:
        return None
    return {"kind": "person", "label": None}


def proposal_item(proposal: Proposal, name: str) -> dict[str, Any]:
    classification = proposal.classification
    is_creation = proposal.diff.parameter.startswith("new_campaign:")
    is_native_write = proposal.diff.parameter.startswith(_NATIVE_WRITE_PARAMETER_PREFIX)
    return {
        # `contracts/api.md` §1 (Revision 2):
        # aditivo con valor por defecto -- un `Proposal` (esta funcion)
        # nunca es un paquete, asi que siempre es `"proposal"`. `package_id`
        # (el otro campo aditivo del contrato) se queda fuera A PROPOSITO:
        # `panel/src/api/schemas/proposals.ts::proposalItemSchema` no lo
        # declara todavia y Zod recorta cualquier clave que no reconoce, asi
        # que emitirlo aqui rompe byte a byte `proposals.contract.test.ts`
        # (comprobado corriendo ese test) -- se suma el mismo dia que el
        # carril del panel declare `package_id: z.string().nullable().
        # optional()` en ese schema.
        "item_kind": "proposal",
        "action_kind": "create_campaign" if is_creation else "update",
        "proposal_id": str(proposal.proposal_id),
        "proposed_by": _proposed_by_view(proposal.proposed_by),
        "entity_ref": str(proposal.diff.entity_ref),
        "entity_name": name,
        "platform": proposal.diff.entity_ref.platform.value,
        "diff": {
            "parametro": proposal.diff.parameter,
            "valor_actual": (
                display_native_write_payload(proposal.diff.before)
                if is_native_write
                else display_value(proposal.diff.before)
            ),
            "valor_propuesto": (
                display_native_write_payload(proposal.diff.after)
                if is_native_write
                else display_value(proposal.diff.after)
            ),
            "diff_hash": proposal.diff.diff_hash,
            "currency": proposal.diff.after.currency
            if isinstance(proposal.diff.after, Money)
            else None,
        },
        "classification": classification.value,
        "urgency": proposal.priority.urgency.value,
        "risk_level": {
            Classification.ROUTINE: "low",
            Classification.IMPORTANT: "medium",
            Classification.CRITICAL: "high",
        }[classification],
        "requires_expansion": is_creation or classification is not Classification.ROUTINE,
        # Bj-5: una escritura nativa es carga libre que el companion no
        # interpreta (`classification.py`) -- el dueño teclea una
        # confirmacion explicita, no un simple click de "Aprobar".
        "requires_typed_confirmation": is_native_write,
        "estimated_impact": {
            "amount": float(proposal.estimated_impact.amount),
            "currency": proposal.estimated_impact.currency,
        },
        "cause": proposal.cause.text,
        "expires_at": proposal.expires_at.isoformat(),
        "postponed_until": proposal.postpone_until.isoformat() if proposal.postpone_until else None,
        "state": proposal.state.value,
    }


async def _entity_names(
    session: AsyncSession, business_id: str, proposals: tuple[Proposal, ...]
) -> dict[str, str]:
    refs = [str(p.diff.entity_ref) for p in proposals]
    if not refs:
        return {}
    rows = await session.execute(
        text(
            "SELECT entity_ref, name FROM ad_entities "
            "WHERE business_id = :business_id AND entity_ref = ANY(:refs)"
        ),
        {"business_id": business_id, "refs": refs},
    )
    return {row.entity_ref: row.name for row in rows}


def group_items(
    proposals: tuple[Proposal, ...],
    names: dict[str, str],
    lens: Lens,
    events: dict[str, tuple[str, str | None]],
) -> list[dict[str, Any]]:
    groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for proposal in proposals:
        event_id = proposal.priority.calendar_event_id
        currency = proposal.estimated_impact.currency
        key = (
            f"evt:{event_id or 'none'}"
            if lens == "calendar_event"
            else f"{proposal.priority.urgency.value}:{proposal.cause_key.as_grouping_key()}"
        )
        # Never sum money in different currencies.
        key = f"{key}:{currency}"
        event = events.get(event_id or "")
        if key not in groups:
            groups[key] = {
                "group_kind": "calendar_event" if lens == "calendar_event" else "cause",
                "cause_key": key,
                "cause": (event[0] if event else "Sin evento asociado")
                if lens == "calendar_event"
                else proposal.cause.text,
                "count": 0,
                "total_impact": {"amount": 0.0, "currency": currency},
                "batch_eligible": lens == "urgency",
                "closes_at": event[1] if lens == "calendar_event" and event else None,
                "proposals": [],
            }
        group = groups[key]
        group["proposals"].append(
            proposal_item(
                proposal, names.get(str(proposal.diff.entity_ref), str(proposal.diff.entity_ref))
            )
        )
        group["count"] += 1
        group["total_impact"]["amount"] += float(proposal.estimated_impact.amount)
        group["batch_eligible"] &= (
            proposal.classification is Classification.ROUTINE
            and proposal.state is ProposalState.PENDING
            and not proposal.diff.parameter.startswith("new_campaign:")
        )
    for group in groups.values():
        group["batch_eligible"] &= 1 < group["count"] <= MAX_BATCH_APPROVE_ITEMS
    return list(groups.values())


async def inbox_page(
    session: AsyncSession, business_id: str, *, lens: Lens, state: str, limit: int, offset: int
) -> dict[str, Any]:
    proposals = await SqlProposalRepository(session).list_by_lens(
        ProposalLens(BusinessId.parse(business_id), states=(state,), limit=limit + 1, offset=offset)
    )
    has_more = len(proposals) > limit
    proposals = proposals[:limit]
    names = await _entity_names(session, business_id, proposals)
    events: dict[str, tuple[str, str | None]] = {}
    if lens == "calendar_event":
        event_rows = await session.execute(
            text(
                "SELECT id, name, window_end FROM calendar_events WHERE business_id = :business_id"
            ),
            {"business_id": business_id},
        )
        events = {
            str(row.id): (row.name, row.window_end.isoformat() if row.window_end else None)
            for row in event_rows
        }
    stats = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE state = 'pending') AS pending_count, "
                "count(*) FILTER (WHERE state = 'postponed') AS deferred_count, "
                "min(expires_at) FILTER (WHERE state = 'pending') AS next_expiring_at "
                "FROM proposals WHERE business_id = :business_id"
            ),
            {"business_id": business_id},
        )
    ).one()
    totals = list(
        await session.execute(
            text(
                "SELECT estimated_impact_currency AS currency, "
                "sum(estimated_impact_amount) AS amount "
                "FROM proposals WHERE business_id = :business_id AND state = :state "
                "GROUP BY estimated_impact_currency"
            ),
            {"business_id": business_id, "state": state},
        )
    )
    return {
        "lens": lens,
        "pending_count": stats.pending_count,
        "deferred_count": stats.deferred_count,
        "total_impact": {"amount": float(totals[0].amount), "currency": totals[0].currency}
        if len(totals) == 1
        else None,
        "next_expiring_at": stats.next_expiring_at.isoformat() if stats.next_expiring_at else None,
        "attention_budget": None,
        "groups": group_items(proposals, names, lens, events),
        "next_cursor": str(offset + limit) if has_more else None,
    }


async def proposal_detail(
    session: AsyncSession, business_id: str, proposal_id: str
) -> dict[str, Any]:
    try:
        identifier = ProposalId.parse(proposal_id)
    except ValueError as exc:
        raise ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.") from exc
    proposal = await SqlProposalRepository(session).get(identifier)
    if proposal is None or str(proposal.business_id) != business_id:
        raise ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")
    names = await _entity_names(session, business_id, (proposal,))
    execution_id = (
        await session.execute(
            text(
                "SELECT id FROM executions "
                "WHERE proposal_id = :proposal AND business_id = :business "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"proposal": proposal_id, "business": business_id},
        )
    ).scalar_one_or_none()
    return {
        **proposal_item(
            proposal, names.get(str(proposal.diff.entity_ref), str(proposal.diff.entity_ref))
        ),
        **creation_details(proposal),
        "cause_key": proposal.cause_key.as_grouping_key(),
        "execution_id": str(execution_id) if execution_id is not None else None,
        "signal_id": proposal.cause.signal_id,
        "signal": None,
        "rule_id": proposal.cause.rule_id,
        "rule_code": proposal.cause.rule_id,
        "rule_hit_rate_pct": None,
        "rule_hit_rate_sample": 0,
        "data_window": ", ".join(dict.fromkeys(item.window_preset for item in proposal.evidence))
        or "No disponible",
        "data_age_minutes": None,
        "estimated_impact_range": None,
        "evidence": [
            {
                "metric": item.metric,
                "unit": None,
                "actual": item.actual,
                "target": item.target,
                "window": item.window_preset,
                "data_age_minutes": None,
                "series": [],
            }
            for item in proposal.evidence
        ],
        "guardrail_verdicts": [],
        "entity_history": [],
        "owner_context": proposal.owner_context,
        "platform_url": None,
    }


def creation_details(proposal: Proposal) -> dict[str, Any]:
    if not proposal.diff.parameter.startswith("new_campaign:"):
        return {"action_kind": "update", "creation_plan": None, "creation_plan_error": None}
    value = proposal.diff.after
    plan = value.get("creation_plan") if isinstance(value, dict) else None
    error = None
    try:
        creation_budget(value, proposal.diff.entity_ref)
    except (CampaignCreationError, TypeError) as exc:
        error = (
            str(exc) if isinstance(exc, CampaignCreationError) else "campaign_creation_plan_invalid"
        )
    return {
        "action_kind": "create_campaign",
        "creation_plan": plan if isinstance(plan, dict) else None,
        "creation_plan_error": error,
    }


def build_proposal_read_router(session_factory: async_sessionmaker[AsyncSession]) -> APIRouter:
    router = APIRouter(tags=["proposals"])

    @router.get("/proposals")
    async def list_inbox(
        request: Request,
        business_id: BusinessScope,
        *,
        lens: Lens = "urgency",
        state: ProposalState = ProposalState.PENDING,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[int, Query(ge=0, le=1000000)] = 0,
    ) -> Response:
        async with session_factory() as session:
            body = await inbox_page(
                session, business_id, lens=lens, state=state.value, limit=limit, offset=cursor
            )
        # Perf (16-sep, item 5): the panel polls this every 45s
        # (`useProposals`, `refetchInterval`) -- most polls see no change,
        # so a matching `If-None-Match` costs a 304 instead of the full
        # inbox body.
        return json_response_with_etag(request, jsonable_encoder(body))

    @router.get("/proposals/{proposal_id}")
    async def get_inbox_proposal(proposal_id: str, business_id: BusinessScope) -> dict[str, Any]:
        async with session_factory() as session:
            return await proposal_detail(session, business_id, proposal_id)

    return router
