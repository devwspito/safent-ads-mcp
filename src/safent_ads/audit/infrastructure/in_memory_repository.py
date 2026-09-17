"""Doble en memoria de `DecisionLogRepository`, para probar
`RecordDecision`/`SearchDecisionLog`/`VerifyDecisionLogChain` sin Postgres.
Reproduce el mismo calculo de cadena que el trigger para que
`VerifyDecisionLogChain` tenga algo coherente que verificar, pero **no**
sustituye a la prueba de integracion: `data-model.md` es explicito en que
"los dobles no modelan CHECKs/UNIQUEs/triggers", y aqui el `payload_text`
se genera con `json.dumps`, no con el `jsonb::text` real de Postgres."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator

from safent_ads.audit.application.ports import DecisionLogFilter, DecisionLogPage
from safent_ads.audit.domain.chain import ChainedRow
from safent_ads.audit.domain.entry import DecisionLogEntry, PendingDecision
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.ids import BusinessId


class InMemoryDecisionLogRepository:
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._entries: list[DecisionLogEntry] = []
        self._last_hash = ""

    async def append(self, pending: PendingDecision) -> DecisionLogEntry:
        seq = len(self._entries) + 1
        payload_text = json.dumps(pending.payload, sort_keys=True)
        entry_hash = hashlib.sha256(f"{seq}|{self._last_hash}|{payload_text}".encode()).hexdigest()
        entry = DecisionLogEntry(
            seq=seq,
            business_id=pending.business_id,
            kind=pending.kind,
            actor_kind=pending.actor_kind,
            payload=pending.payload,
            prev_hash=self._last_hash,
            entry_hash=entry_hash,
            occurred_at=self._clock.now(),
            entity_ref=pending.entity_ref,
            actor_id=pending.actor_id,
            proposal_id=pending.proposal_id,
        )
        self._entries.append(entry)
        self._last_hash = entry_hash
        return entry

    async def search(self, criteria: DecisionLogFilter) -> DecisionLogPage:
        candidates = [e for e in self._entries if e.business_id == criteria.business_id]
        if criteria.kind is not None:
            candidates = [e for e in candidates if e.kind == criteria.kind]
        if criteria.entity_ref is not None:
            candidates = [e for e in candidates if e.entity_ref == criteria.entity_ref]
        if criteria.since is not None:
            candidates = [e for e in candidates if e.occurred_at >= criteria.since]
        if criteria.until is not None:
            candidates = [e for e in candidates if e.occurred_at <= criteria.until]
        if criteria.cursor_seq is not None:
            candidates = [e for e in candidates if e.seq < criteria.cursor_seq]

        candidates.sort(key=lambda e: e.seq, reverse=True)
        page = tuple(candidates[: criteria.limit])
        next_cursor = page[-1].seq if len(page) == criteria.limit else None
        return DecisionLogPage(entries=page, next_cursor_seq=next_cursor)

    async def get_by_seq(self, business_id: BusinessId, seq: int) -> DecisionLogEntry | None:
        for entry in self._entries:
            if entry.business_id == business_id and entry.seq == seq:
                return entry
        return None

    async def stream_for_verification(self) -> AsyncIterator[ChainedRow]:
        for entry in self._entries:
            yield ChainedRow(
                seq=entry.seq,
                prev_hash=entry.prev_hash,
                entry_hash=entry.entry_hash,
                payload_text=json.dumps(entry.payload, sort_keys=True),
            )
