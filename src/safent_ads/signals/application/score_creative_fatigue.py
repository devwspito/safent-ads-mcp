"""`ScoreCreativeFatigue` (plan.md §5, tasks.md T037): M16 (CTR vs linea base
14D) y M21 (hook/hold rate) sobre una creatividad. Reutiliza las puertas ya
evaluadas por el llamante (misma entidad, mismo ciclo)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.application.ports import CreativeSignalRepository, MetricWindowRepository
from safent_ads.signals.domain.gate_verdict import GateVerdict
from safent_ads.signals.domain.signal import CreativeSignal
from safent_ads.signals.domain.signal_engine import RuleContext, evaluate_m16, evaluate_m21
from safent_ads.signals.domain.window_span import WindowSpan


@dataclass(frozen=True, kw_only=True, slots=True)
class ScoreCreativeFatigueRequest:
    entity_ref: EntityRef
    currency: str
    gate_verdicts: tuple[GateVerdict, ...]
    as_of: datetime


class ScoreCreativeFatigue:
    def __init__(self, windows: MetricWindowRepository, signals: CreativeSignalRepository) -> None:
        self._windows = windows
        self._signals = signals

    async def execute(self, request: ScoreCreativeFatigueRequest) -> CreativeSignal | None:
        window_7d = await self._windows.fetch_window(
            entity_ref=request.entity_ref, span=WindowSpan.D7, as_of=request.as_of
        )
        window_14d = await self._windows.fetch_window(
            entity_ref=request.entity_ref, span=WindowSpan.D14, as_of=request.as_of
        )
        context = RuleContext(
            entity_ref=request.entity_ref,
            currency=request.currency,
            gate_verdicts=request.gate_verdicts,
            as_of=request.as_of,
        )
        signal = evaluate_m16(context, window_7d=window_7d, window_14d=window_14d) or evaluate_m21(
            context, window_7d=window_7d
        )
        if signal is not None:
            await self._signals.save(signal)
        return signal
