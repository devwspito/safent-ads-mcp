"""`/api/v1/decision-log*` (contracts/rest-api.md). `require_business_access`
protege la busqueda y el detalle (C-27); `verify` no lleva `business_id`
porque la cadena de `decision_log` es global a todo el proceso, no por
negocio (0002_audit_chain.py: un unico `seq` para toda la tabla)."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Request

from safent_ads.audit.application.ports import DecisionLogFilter
from safent_ads.audit.application.search_decision_log import SearchDecisionLog
from safent_ads.audit.application.verify_decision_log_chain import VerifyDecisionLogChain
from safent_ads.audit.domain.chain import ChainVerifier
from safent_ads.audit.domain.entry import DecisionKind, DecisionLogEntry
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.audit.presentation.schemas import (
    ChainVerificationResponse,
    DecisionLogEntryResponse,
    DecisionLogPageResponse,
)
from safent_ads.composition.container import Container
from safent_ads.iam.presentation.dependencies import CURRENT_OWNER, require_business_access
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.shared.ids import BusinessId, EntityRef

_REQUIRE_BUSINESS_ACCESS = Depends(require_business_access)

router = APIRouter(prefix="/api/v1/decision-log", tags=["audit"])


def _to_response(entry: DecisionLogEntry) -> DecisionLogEntryResponse:
    return DecisionLogEntryResponse(
        seq=entry.seq,
        business_id=entry.business_id.value,
        kind=entry.kind,
        actor_kind=entry.actor_kind,
        actor_id=entry.actor_id,
        entity_ref=str(entry.entity_ref) if entry.entity_ref else None,
        proposal_id=entry.proposal_id,
        payload=dict(entry.payload),
        prev_hash=entry.prev_hash,
        entry_hash=entry.entry_hash,
        occurred_at=entry.occurred_at,
    )


@router.get("", response_model=DecisionLogPageResponse)
async def search_decision_log(
    request: Request,
    *,
    business_id: uuid.UUID = _REQUIRE_BUSINESS_ACCESS,
    event_type: DecisionKind | None = None,
    entity_ref: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    cursor: int | None = None,
) -> DecisionLogPageResponse:
    container: Container = request.app.state.container
    criteria = DecisionLogFilter(
        business_id=BusinessId(business_id),
        kind=event_type,
        entity_ref=EntityRef.parse(entity_ref) if entity_ref else None,
        since=since,
        until=until,
        limit=limit,
        cursor_seq=cursor,
    )
    async with container.session_factory() as db_session:
        page = await SearchDecisionLog(SqlDecisionLogRepository(db_session)).execute(criteria)

    return DecisionLogPageResponse(
        entries=[_to_response(entry) for entry in page.entries],
        next_cursor_seq=page.next_cursor_seq,
    )


@router.get("/verify", response_model=ChainVerificationResponse)
async def verify_chain(
    request: Request, _owner: object = CURRENT_OWNER
) -> ChainVerificationResponse:
    container: Container = request.app.state.container
    async with container.session_factory() as db_session:
        report = await VerifyDecisionLogChain(
            SqlDecisionLogRepository(db_session), ChainVerifier(), container.clock
        ).execute()

    return ChainVerificationResponse(
        chain_ok=report.chain_ok,
        verified_through_seq=report.verified_through_seq,
        checked_at=report.checked_at,
    )


@router.get("/{seq}", response_model=DecisionLogEntryResponse)
async def get_decision_log_entry(
    seq: int, request: Request, business_id: uuid.UUID = _REQUIRE_BUSINESS_ACCESS
) -> DecisionLogEntryResponse:
    container: Container = request.app.state.container
    async with container.session_factory() as db_session:
        entry = await SqlDecisionLogRepository(db_session).get_by_seq(
            BusinessId(business_id), seq
        )

    if entry is None:
        raise ApiError(status_code=404, code="NOT_FOUND", message="No encontrado.")
    return _to_response(entry)
