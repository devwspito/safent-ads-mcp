"""Base de eventos de dominio y los puertos que los transportan (plan.md N0):
`DomainEventBus` para comunicacion entre contextos, `DecisionRecorder` para
anexarlos al `decision_log` (audit, data-model.md)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    """Base de todo evento de dominio. Toda subclase concreta anade sus
    propios atributos; estos tres son obligatorios en cualquier evento
    (data-model.md: "Todos llevan business_id, occurred_at y cycle_id")."""

    business_id: BusinessId
    occurred_at: datetime
    cycle_id: str | None = None


class DomainEventBus(Protocol):
    """Puerto: publica eventos de dominio para que `composition` los enrute
    entre contextos sin que estos se importen entre si (plan.md §4)."""

    async def publish(self, event: DomainEvent) -> None: ...


class DecisionRecorder(Protocol):
    """Puerto: anexa un evento al `decision_log` solo-anexable. Se llama
    siempre, en exito y en fallo (plan.md §6.7)."""

    async def record(self, event: DomainEvent) -> None: ...
