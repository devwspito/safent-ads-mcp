"""Dobles en memoria de `SignalRepository` y `CreativeSignalRepository`."""

from __future__ import annotations

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.domain.signal import CreativeSignal, Signal


class InMemorySignalRepository:
    def __init__(self) -> None:
        self.saved: list[Signal] = []

    async def save(self, signal: Signal) -> None:
        self.saved.append(signal)

    async def find_latest_for_entity(self, *, entity_ref: EntityRef) -> Signal | None:
        matching = [s for s in self.saved if s.entity_ref == entity_ref]
        return matching[-1] if matching else None


class InMemoryCreativeSignalRepository:
    def __init__(self) -> None:
        self.saved: list[CreativeSignal] = []

    async def save(self, signal: CreativeSignal) -> None:
        self.saved.append(signal)

    async def find_latest_for_entity(self, *, entity_ref: EntityRef) -> CreativeSignal | None:
        matching = [s for s in self.saved if s.entity_ref == entity_ref]
        return matching[-1] if matching else None
