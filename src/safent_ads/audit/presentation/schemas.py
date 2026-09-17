"""DTOs de `/api/v1/decision-log*` (contracts/rest-api.md)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from safent_ads.audit.domain.entry import ActorKind, DecisionKind


class DecisionLogEntryResponse(BaseModel):
    seq: int
    business_id: uuid.UUID
    kind: DecisionKind
    actor_kind: ActorKind
    actor_id: str | None
    entity_ref: str | None
    proposal_id: uuid.UUID | None
    payload: dict[str, object]
    prev_hash: str
    entry_hash: str
    occurred_at: datetime


class DecisionLogPageResponse(BaseModel):
    entries: list[DecisionLogEntryResponse]
    next_cursor_seq: int | None


class ChainVerificationResponse(BaseModel):
    chain_ok: bool
    verified_through_seq: int | None
    checked_at: datetime
