"""`AuditReadPort` real (integracion, wiring2) sobre `decision_log`
(`0002_audit_chain.py`). Envoltorio delgado de `SqlDecisionLogRepository`
(`audit/infrastructure/sql_repository.py`): `audit` ya expone un puerto de
aplicacion (`DecisionLogRepository`) con la busqueda paginada y el
aislamiento por `business_id` resueltos -- aqui solo se traduce
`DecisionLogEntry`/`DecisionLogPage` a los DTOs tipados de
`mcp.application.dto`, mismo patron que `SqlBrandReadPort` envolviendo
`SqlBrandKitRepository`.

`payload: dict[str, str]` del contrato MCP aplana el `JsonValue` heterogeneo
del payload real: los valores que ya son `str` se dejan tal cual, el resto
se serializa con `json.dumps` para no perder informacion ni inventar un
esquema que `decision_log` no tiene."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.audit.application.ports import DecisionLogFilter
from safent_ads.audit.domain.entry import DecisionKind, DecisionLogEntry
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.mcp.application.dto import DecisionLogEntryDetail, DecisionLogEntrySummary, Page
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.shared.ids import BusinessId, EntityRef


class SqlAuditReadPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def search_decision_log(
        self,
        business_id: str,
        *,
        since: datetime,
        until: datetime,
        event_type: str | None,
        entity_ref: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[DecisionLogEntrySummary]:
        criteria = DecisionLogFilter(
            business_id=BusinessId.parse(business_id),
            kind=DecisionKind(event_type) if event_type else None,
            entity_ref=EntityRef.parse(entity_ref) if entity_ref else None,
            since=since,
            until=until,
            limit=limit,
            cursor_seq=int(cursor) if cursor else None,
        )
        async with self._session_factory() as session:
            result = await SqlDecisionLogRepository(session).search(criteria)
        return Page(
            items=[_summary(entry) for entry in result.entries],
            cursor=(
                str(result.next_cursor_seq) if result.next_cursor_seq is not None else None
            ),
        )

    async def get_decision_log_entry(
        self, business_id: str, seq: int
    ) -> DecisionLogEntryDetail:
        async with self._session_factory() as session:
            entry = await SqlDecisionLogRepository(session).get_by_seq(
                BusinessId.parse(business_id), seq
            )
        if entry is None:
            raise EntityNotFoundError(f"entrada {seq} aun no disponible")
        return DecisionLogEntryDetail(
            summary=_summary(entry),
            payload=_stringify_payload(entry.payload),
            prev_hash=entry.prev_hash,
        )


def _summary(entry: DecisionLogEntry) -> DecisionLogEntrySummary:
    return DecisionLogEntrySummary(
        seq=entry.seq,
        occurred_at=entry.occurred_at,
        event_type=entry.kind.value,
        entity_ref=str(entry.entity_ref) if entry.entity_ref is not None else None,
        entry_hash=entry.entry_hash,
    )


def _stringify_payload(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    return {
        key: value if isinstance(value, str) else json.dumps(value)
        for key, value in payload.items()
    }
