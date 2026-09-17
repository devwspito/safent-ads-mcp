"""Resultado de un ciclo de orquestacion (plan.md §7: "cada ciclo es
idempotente, lleva `cycle_id`, escribe traza y no comparte estado en
memoria con otro"). Un `CycleReport` nunca oculta un fallo parcial: lista
un `StepResult` por paso intentado, exito o fallo."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class StepOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StepResult:
    step_name: str
    business_id: str | None
    outcome: StepOutcome
    attempts: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CycleReport:
    cycle_name: str
    cycle_id: str
    started_at: datetime
    finished_at: datetime
    results: tuple[StepResult, ...]

    @property
    def all_succeeded(self) -> bool:
        return all(result.outcome is StepOutcome.SUCCESS for result in self.results)

    @property
    def failed_results(self) -> tuple[StepResult, ...]:
        return tuple(result for result in self.results if result.outcome is StepOutcome.FAILED)
