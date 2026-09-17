"""Adaptador SQL de `DecisionLogRepository` sobre SQLAlchemy async
(0002_audit_chain.py). SQL crudo via `text()`, no ORM: `decision_log` es
solo-anexable con columnas calculadas por trigger (`prev_hash`,
`entry_hash`), un mal ajuste para un modelo declarativo que asuma que el
cliente controla esas columnas.

Siempre pide `payload::text` en SQL en vez de fiarse de una decodificacion
automatica jsonb->dict del driver: es la unica forma de garantizar que el
texto que ve Python es *exactamente* el que vio el trigger al calcular
`entry_hash` (audit/domain/chain.py), y de paso evita depender de si la
capa asyncpg/SQLAlchemy en uso registra o no un codec para `jsonb`."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.audit.application.ports import DecisionLogFilter, DecisionLogPage
from safent_ads.audit.domain.chain import ChainedRow
from safent_ads.audit.domain.entry import (
    ActorKind,
    DecisionKind,
    DecisionLogEntry,
    PendingDecision,
)
from safent_ads.shared.ids import BusinessId, EntityRef

_INSERT_SQL = text(
    """
    INSERT INTO decision_log (business_id, event_type, entity_ref, actor_kind, actor_id,
                               proposal_id, payload)
    VALUES (:business_id, :event_type, :entity_ref, :actor_kind, :actor_id, :proposal_id,
            CAST(:payload AS JSONB))
    RETURNING seq, prev_hash, entry_hash, occurred_at
    """
)

_SEARCH_SQL_TEMPLATE = """
    SELECT seq, business_id, event_type, entity_ref, actor_kind, actor_id, proposal_id,
           payload::text AS payload_text, prev_hash, entry_hash, occurred_at
    FROM decision_log
    WHERE business_id = :business_id
    {filters}
    ORDER BY seq DESC
    LIMIT :limit
"""

_GET_BY_SEQ_SQL = text(
    """
    SELECT seq, business_id, event_type, entity_ref, actor_kind, actor_id, proposal_id,
           payload::text AS payload_text, prev_hash, entry_hash, occurred_at
    FROM decision_log
    WHERE business_id = :business_id AND seq = :seq
    """
)

_STREAM_FOR_VERIFICATION_SQL = text(
    "SELECT seq, prev_hash, entry_hash, payload::text AS payload_text "
    "FROM decision_log ORDER BY seq"
)


def _optional_str(value: EntityRef | uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def _row_to_entry(row: Any) -> DecisionLogEntry:  # noqa: ANN401 - fila heterogenea de SQLAlchemy
    return DecisionLogEntry(
        seq=row.seq,
        business_id=BusinessId.parse(str(row.business_id)),
        kind=DecisionKind(row.event_type),
        actor_kind=ActorKind(row.actor_kind),
        payload=json.loads(row.payload_text),
        prev_hash=row.prev_hash,
        entry_hash=row.entry_hash,
        occurred_at=row.occurred_at,
        entity_ref=EntityRef.parse(row.entity_ref) if row.entity_ref else None,
        actor_id=row.actor_id,
        proposal_id=uuid.UUID(str(row.proposal_id)) if row.proposal_id else None,
    )


def _build_search_query(criteria: DecisionLogFilter) -> tuple[Any, dict[str, Any]]:
    filters: list[str] = []
    params: dict[str, Any] = {
        "business_id": str(criteria.business_id),
        "limit": criteria.limit,
    }
    if criteria.kind is not None:
        filters.append("AND event_type = :kind")
        params["kind"] = criteria.kind.value
    if criteria.entity_ref is not None:
        filters.append("AND entity_ref = :entity_ref")
        params["entity_ref"] = str(criteria.entity_ref)
    if criteria.since is not None:
        filters.append("AND occurred_at >= :since")
        params["since"] = criteria.since
    if criteria.until is not None:
        filters.append("AND occurred_at <= :until")
        params["until"] = criteria.until
    if criteria.cursor_seq is not None:
        filters.append("AND seq < :cursor_seq")
        params["cursor_seq"] = criteria.cursor_seq

    sql = text(_SEARCH_SQL_TEMPLATE.format(filters="\n    ".join(filters)))
    return sql, params


class SqlDecisionLogRepository:
    """Implementa `DecisionLogRepository` (audit/application/ports.py)
    estructuralmente, igual que `SystemClock` implementa `Clock`
    (shared/clock.py) -- sin heredar del `Protocol`.

    Vive dentro de la transaccion del `AsyncSession` que le pasan; no
    hace `commit()` -- el limite de transaccion lo decide quien orquesta
    (composition/presentation), igual que las capas de uso de `iam`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, pending: PendingDecision) -> DecisionLogEntry:
        result = await self._session.execute(
            _INSERT_SQL,
            {
                "business_id": str(pending.business_id),
                "event_type": pending.kind.value,
                "entity_ref": _optional_str(pending.entity_ref),
                "actor_kind": pending.actor_kind.value,
                "actor_id": pending.actor_id,
                "proposal_id": _optional_str(pending.proposal_id),
                "payload": json.dumps(pending.payload, sort_keys=True),
            },
        )
        row = result.one()
        return DecisionLogEntry(
            seq=row.seq,
            business_id=pending.business_id,
            kind=pending.kind,
            actor_kind=pending.actor_kind,
            payload=pending.payload,
            prev_hash=row.prev_hash,
            entry_hash=row.entry_hash,
            occurred_at=row.occurred_at,
            entity_ref=pending.entity_ref,
            actor_id=pending.actor_id,
            proposal_id=pending.proposal_id,
        )

    async def search(self, criteria: DecisionLogFilter) -> DecisionLogPage:
        sql, params = _build_search_query(criteria)
        result = await self._session.execute(sql, params)
        rows = result.all()
        entries = tuple(_row_to_entry(row) for row in rows)
        next_cursor = entries[-1].seq if len(entries) == criteria.limit else None
        return DecisionLogPage(entries=entries, next_cursor_seq=next_cursor)

    async def get_by_seq(self, business_id: BusinessId, seq: int) -> DecisionLogEntry | None:
        result = await self._session.execute(
            _GET_BY_SEQ_SQL, {"business_id": str(business_id), "seq": seq}
        )
        row = result.one_or_none()
        return None if row is None else _row_to_entry(row)

    async def stream_for_verification(self) -> AsyncIterator[ChainedRow]:
        result = await self._session.stream(_STREAM_FOR_VERIFICATION_SQL)
        async for row in result:
            yield ChainedRow(
                seq=row.seq,
                prev_hash=row.prev_hash,
                entry_hash=row.entry_hash,
                payload_text=row.payload_text,
            )
