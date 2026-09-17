"""`Budget`: `Money` + `BudgetKind` (data-model.md `AdEntity.Budget`)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from safent_ads.accounts.domain.money import Money


class BudgetKind(StrEnum):
    """Alcance del presupuesto sobre la entidad publicitaria."""

    DAILY = "daily"
    LIFETIME = "lifetime"
    SHARED = "shared"


@dataclass(frozen=True, slots=True)
class Budget:
    """Presupuesto de una `AdEntity`. `SHARED` implica que el importe vive en
    `SharedBudgetRef`, no en esta entidad (data-model.md)."""

    amount: Money
    kind: BudgetKind
