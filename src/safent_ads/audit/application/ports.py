"""Puertos de `audit` (plan.md N0): `RecordDecision`/`SearchDecisionLog`
dependen de `DecisionLogRepository`, nunca de SQLAlchemy directamente."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from safent_ads.audit.domain.chain import ChainedRow
from safent_ads.audit.domain.entry import DecisionKind, DecisionLogEntry, PendingDecision
from safent_ads.shared.errors import ApplicationError
from safent_ads.shared.ids import BusinessId, EntityRef

_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 50


class InvalidDecisionLogFilterError(ApplicationError):
    """Filtro de busqueda fuera de rango (p. ej. `limit` invalido)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionLogFilter:
    """Criterios de `GET /decision-log` (contracts/rest-api.md)."""

    business_id: BusinessId
    kind: DecisionKind | None = None
    entity_ref: EntityRef | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = _DEFAULT_PAGE_SIZE
    cursor_seq: int | None = None

    def __post_init__(self) -> None:
        if not (1 <= self.limit <= _MAX_PAGE_SIZE):
            raise InvalidDecisionLogFilterError(
                f"limit debe estar entre 1 y {_MAX_PAGE_SIZE}: {self.limit}"
            )
        if self.since is not None and self.until is not None and self.since > self.until:
            raise InvalidDecisionLogFilterError("since no puede ser posterior a until")


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionLogPage:
    entries: tuple[DecisionLogEntry, ...]
    next_cursor_seq: int | None


class DecisionLogRepository(Protocol):
    """Puerto: la unica forma de tocar `decision_log` desde `application`."""

    async def append(self, pending: PendingDecision) -> DecisionLogEntry: ...

    async def search(self, criteria: DecisionLogFilter) -> DecisionLogPage: ...

    async def get_by_seq(self, business_id: BusinessId, seq: int) -> DecisionLogEntry | None: ...

    def stream_for_verification(self) -> AsyncIterator[ChainedRow]: ...
