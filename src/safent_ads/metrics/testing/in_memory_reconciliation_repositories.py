"""Dobles en memoria de los puertos de `ReconcilePlatformVsCrm` (T116):
`ActionableSignalPort`, `CrmConversionsPort`, `SignalContradictionRecorder`.
Mismo patron que `optimization.testing.in_memory_repositories` para T199 --
`seed*` puebla el estado, el caso de uso solo ve los puertos."""

from __future__ import annotations

from datetime import date, datetime

from safent_ads.metrics.application.ports import ActionableSignalRef
from safent_ads.metrics.domain.reconciliation import SignalContradicted
from safent_ads.shared.ids import BusinessId, EntityRef


class InMemoryActionableSignalPort:
    def __init__(self) -> None:
        self._by_business: dict[BusinessId, list[ActionableSignalRef]] = {}

    def seed(self, *, business_id: BusinessId, refs: list[ActionableSignalRef]) -> None:
        self._by_business.setdefault(business_id, []).extend(refs)

    async def list_unresolved(
        self, *, business_id: BusinessId, cutoff: datetime
    ) -> tuple[ActionableSignalRef, ...]:
        del cutoff
        return tuple(self._by_business.get(business_id, []))

    def remove(self, signal_id: str) -> None:
        """Simula que `contradicted_at` ya quedo escrito: el proximo ciclo
        no vuelve a ver la senal (idempotencia)."""
        for refs in self._by_business.values():
            refs[:] = [ref for ref in refs if ref.signal_id != signal_id]


class InMemoryCrmConversionsPort:
    def __init__(self) -> None:
        self._counts: dict[EntityRef, int] = {}

    def seed(self, *, entity_ref: EntityRef, count: int) -> None:
        self._counts[entity_ref] = count

    async def count_confirmed_conversions(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        window_start: date,
        window_end: date,
    ) -> int:
        del business_id, window_start, window_end
        return self._counts.get(entity_ref, 0)


class InMemorySignalContradictionRecorder:
    def __init__(self) -> None:
        self.recorded: list[SignalContradicted] = []

    async def record(self, *, event: SignalContradicted) -> None:
        self.recorded.append(event)
