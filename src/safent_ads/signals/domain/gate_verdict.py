"""`GateVerdict` (data-model.md §Signal: 'si una puerta falla se emite
`kind = HOLD` con `gate_reason` y no es accionable')."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GateName(StrEnum):
    LEARNING = "learning"
    MIN_DATA = "min_data"
    ATTRIBUTION_LAG = "attribution_lag"
    COOLDOWN = "cooldown"


@dataclass(frozen=True, kw_only=True, slots=True)
class GateVerdict:
    gate: GateName
    passed: bool
    reason: str | None = None

    @classmethod
    def ok(cls, gate: GateName) -> GateVerdict:
        return cls(gate=gate, passed=True)

    @classmethod
    def blocked(cls, gate: GateName, reason: str) -> GateVerdict:
        return cls(gate=gate, passed=False, reason=reason)
