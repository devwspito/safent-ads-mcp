"""`Signal` (data-model.md §Signal, tabla `signals`; spec.md FR-6).

Invariante: toda senal lleva puerta superada, ventana, causa en una frase y
`money_at_stake`; si una puerta falla, `kind = HOLD` con `gate_reason`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.domain.cause import Cause
from safent_ads.signals.domain.gate_verdict import GateVerdict
from safent_ads.signals.domain.value_objects import Evidence, MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan


class SignalKind(StrEnum):
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"
    EXIT = "exit"


class CreativeSignalKind(StrEnum):
    FATIGUE = "fatigue"
    WINNER = "winner"
    LOSER = "loser"


@dataclass(frozen=True, kw_only=True, slots=True)
class Signal:
    entity_ref: EntityRef
    kind: SignalKind
    strength: SignalStrength
    cause: Cause
    cause_sentence: str
    span: WindowSpan
    money_at_stake: MoneyAtStake
    evidence: Evidence
    gate_verdicts: tuple[GateVerdict, ...]
    emitted_at: datetime
    rule_code: str
    # `None` mientras la senal solo vive en memoria (recien emitida por
    # `signal_engine`, `id` lo asigna Postgres al persistir); una vez leida
    # de vuelta (`SqlSignalRepository.find_latest_for_entity`) lleva su
    # `signals.id` real, que `rule_step.py` necesita para enlazar la
    # propuesta que dispare con la senal que la causo.
    signal_id: str | None = None


@dataclass(frozen=True, kw_only=True, slots=True)
class CreativeSignal:
    entity_ref: EntityRef
    kind: CreativeSignalKind
    strength: SignalStrength
    cause: Cause
    cause_sentence: str
    span: WindowSpan
    money_at_stake: MoneyAtStake
    evidence: Evidence
    gate_verdicts: tuple[GateVerdict, ...]
    emitted_at: datetime
    rule_code: str
    signal_id: str | None = None
