"""Forma declarativa de una condicion de regla (tasks.md T038: 'condicion en
una forma declarativa que definas — metrica, comparador, ventana, umbral o
% relativo a linea base').

Una `Condition` es una conjuncion (AND) de `ConditionClause`. Multiplos del
catalogo ("5-10x target CPA", "2x baseline") se codifican como
`TARGET_RELATIVE_PCT`/`BASELINE_RELATIVE_PCT` con `value` en puntos
porcentuales (5x = 500)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from safent_ads.rules.domain.errors import EmptyConditionError


class Comparator(StrEnum):
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    EQ = "eq"
    NE = "ne"


class ThresholdKind(StrEnum):
    ABSOLUTE = "absolute"
    TARGET_RELATIVE_PCT = "target_relative_pct"
    BASELINE_RELATIVE_PCT = "baseline_relative_pct"


@dataclass(frozen=True, kw_only=True, slots=True)
class ConditionClause:
    metric: str
    comparator: Comparator
    window: str
    threshold_kind: ThresholdKind
    value: float


@dataclass(frozen=True, slots=True)
class Condition:
    clauses: tuple[ConditionClause, ...]

    def __post_init__(self) -> None:
        if not self.clauses:
            raise EmptyConditionError("Condition sin clausulas")
