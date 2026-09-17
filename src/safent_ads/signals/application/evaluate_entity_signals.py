"""`EvaluateEntitySignals` (plan.md §5, tasks.md T037): puertas -> catalogo ->
persistencia. Ejemplo de orquestacion; `rules/application.EvaluateRules`
(otro lane) decide mas adelante que reglas del catalogo completo aplican a
que entidad — aqui se prueban las representativas ya cubiertas por
`signal_engine` en el orden de prioridad BUY/SELL/EXIT del catalogo."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.application.ports import MetricWindowRepository, SignalRepository
from safent_ads.signals.domain.gate_verdict import GateVerdict
from safent_ads.signals.domain.gates import (
    AttributionLagGate,
    CooldownGate,
    LearningGate,
    LearningStatus,
    MinDataGate,
)
from safent_ads.signals.domain.metric_window import MetricWindow
from safent_ads.signals.domain.signal import Signal
from safent_ads.signals.domain.signal_engine import (
    RuleContext,
    evaluate_g01,
    evaluate_g04,
    evaluate_g06,
    evaluate_m05,
    evaluate_m07,
    evaluate_m09,
    evaluate_m13,
    hold_signal,
)
from safent_ads.signals.domain.window_span import WindowSpan


@dataclass(frozen=True, kw_only=True, slots=True)
class SignalTargets:
    currency: str
    target_cpa_minor: int
    target_roas: float


@dataclass(frozen=True, kw_only=True, slots=True)
class GateContext:
    learning_status: LearningStatus
    last_change_at: datetime | None
    cooldown: timedelta
    median_lag_days: int
    min_spend_multiple: float
    min_impressions: int
    min_conversions: int


@dataclass(frozen=True, kw_only=True, slots=True)
class EvaluateEntitySignalsRequest:
    entity_ref: EntityRef
    targets: SignalTargets
    gate_context: GateContext
    as_of: datetime


class EvaluateEntitySignals:
    def __init__(self, windows: MetricWindowRepository, signals: SignalRepository) -> None:
        self._windows = windows
        self._signals = signals

    async def execute(self, request: EvaluateEntitySignalsRequest) -> Signal:
        window_7d = await self._windows.fetch_window(
            entity_ref=request.entity_ref, span=WindowSpan.D7, as_of=request.as_of
        )
        gate_verdicts = self._evaluate_gates(request, window_7d)
        context = RuleContext(
            entity_ref=request.entity_ref,
            currency=request.targets.currency,
            gate_verdicts=gate_verdicts,
            as_of=request.as_of,
        )
        signal = (
            hold_signal(context, span=WindowSpan.D7)
            if any(not verdict.passed for verdict in gate_verdicts)
            else await self._first_matching_signal(context, request, window_7d)
        )
        await self._signals.save(signal)
        return signal

    def _evaluate_gates(
        self, request: EvaluateEntitySignalsRequest, window_7d: MetricWindow
    ) -> tuple[GateVerdict, ...]:
        lag_boundary = request.as_of.date() - timedelta(days=request.gate_context.median_lag_days)
        return (
            LearningGate.evaluate(request.gate_context.learning_status),
            MinDataGate.evaluate(
                spend_minor=window_7d.spend_minor,
                target_cpa_minor=request.targets.target_cpa_minor,
                impressions=window_7d.impressions,
                conversions=window_7d.conversions,
                min_spend_multiple=request.gate_context.min_spend_multiple,
                min_impressions=request.gate_context.min_impressions,
                min_conversions=request.gate_context.min_conversions,
            ),
            AttributionLagGate.evaluate(
                window_end=lag_boundary,
                median_lag_days=request.gate_context.median_lag_days,
                as_of=request.as_of.date(),
            ),
            CooldownGate.evaluate(
                last_change_at=request.gate_context.last_change_at,
                cooldown=request.gate_context.cooldown,
                as_of=request.as_of,
            ),
        )

    async def _first_matching_signal(
        self, context: RuleContext, request: EvaluateEntitySignalsRequest, window_7d: MetricWindow
    ) -> Signal:
        window_3d = await self._windows.fetch_window(
            entity_ref=request.entity_ref, span=WindowSpan.D3, as_of=request.as_of
        )
        window_30d = await self._windows.fetch_window(
            entity_ref=request.entity_ref, span=WindowSpan.D30, as_of=request.as_of
        )
        candidates: Sequence[Signal | None] = [
            evaluate_m05(
                context,
                window_3d=window_3d,
                window_7d=window_7d,
                target_roas=request.targets.target_roas,
            ),
            evaluate_m07(
                context, window_7d=window_7d, target_cpa_minor=request.targets.target_cpa_minor
            ),
            evaluate_m09(
                context, window_7d=window_7d, target_cpa_minor=request.targets.target_cpa_minor
            ),
            evaluate_m13(context, window_7d=window_7d),
            evaluate_g01(context, window_7d=window_7d, target_roas=request.targets.target_roas),
            evaluate_g04(
                context, window_30d=window_30d, target_cpa_minor=request.targets.target_cpa_minor
            ),
            evaluate_g06(
                context, window_30d=window_30d, target_cpa_minor=request.targets.target_cpa_minor
            ),
        ]
        return next((signal for signal in candidates if signal is not None), None) or hold_signal(
            context, span=WindowSpan.D7
        )
