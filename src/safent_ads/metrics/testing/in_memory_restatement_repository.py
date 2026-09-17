"""Doble en memoria de `RestatementRepository`: solo-anexable."""

from __future__ import annotations

from collections.abc import Sequence

from safent_ads.metrics.domain.restatement import Restatement
from safent_ads.shared.ids import EntityRef


class InMemoryRestatementRepository:
    def __init__(self) -> None:
        self._restatements: list[Restatement] = []

    async def record(self, restatement: Restatement) -> None:
        self._restatements.append(restatement)

    async def list_for_entity(self, *, entity_ref: EntityRef) -> Sequence[Restatement]:
        return [r for r in self._restatements if r.entity_ref == entity_ref]
