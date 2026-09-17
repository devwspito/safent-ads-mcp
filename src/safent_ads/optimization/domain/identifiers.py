"""Identificadores de `optimization` (profitability-engine.md §3-§7,
data-model.md handoff `0017_optimization`)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MarginalEstimateId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> MarginalEstimateId:
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, raw: str) -> MarginalEstimateId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class AllocationPlanId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> AllocationPlanId:
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, raw: str) -> AllocationPlanId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class ResponseCurveId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> ResponseCurveId:
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, raw: str) -> ResponseCurveId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class ExperimentId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> ExperimentId:
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, raw: str) -> ExperimentId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class SignalOutcomeId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> SignalOutcomeId:
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, raw: str) -> SignalOutcomeId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class CalibrationAdjustmentId:
    value: uuid.UUID

    @classmethod
    def new(cls) -> CalibrationAdjustmentId:
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, raw: str) -> CalibrationAdjustmentId:
        return cls(uuid.UUID(raw))

    def __str__(self) -> str:
        return str(self.value)
