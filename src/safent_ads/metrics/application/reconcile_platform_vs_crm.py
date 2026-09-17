"""`ReconcilePlatformVsCrm` (tasks.md T116; profitability-engine.md §2: "el
CRM es la verdad"; FR-3/FR-5): por senal accionable pendiente de
contradiccion, compara lo que reporto la plataforma contra lo que el CRM
confirma en la MISMA ventana de evidencia.

No duplica `economics.ComputePlatformDivergence` (shrinkage sobre 8 semanas
cerradas, pensado para decisiones de cartera): reusa sus MISMOS insumos
(`metrics.MetricFactRepository`, conteo de `crm`) a traves de los puertos
que este modulo declara -- el adaptador concreto (`orchestration`, T078)
envuelve los repositorios SQL reales que `ComputePlatformDivergence` ya usa,
sin reimplementar la suma de conversiones ni el conteo del CRM.

Rezago de atribucion (spec.md Edge Cases: "la conversion de negocio del CRM
llega dias despues -> reevaluacion, nunca contradiccion silenciosa"): el
llamador (`orchestration`, T078) pasa un `cutoff` con margen de rezago --
este caso de uso no decide ese margen, solo actua sobre lo que
`ActionableSignalPort` ya filtro."""

from __future__ import annotations

from datetime import datetime

from safent_ads.metrics.application.ports import (
    ActionableSignalPort,
    ActionableSignalRef,
    CrmConversionsPort,
    MetricFactRepository,
    SignalContradictionRecorder,
)
from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.reconciliation import ConversionReconciliation, SignalContradicted
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId


class ReconcilePlatformVsCrm:
    def __init__(
        self,
        *,
        metrics: MetricFactRepository,
        crm_conversions: CrmConversionsPort,
        actionable_signals: ActionableSignalPort,
        recorder: SignalContradictionRecorder,
        clock: Clock,
    ) -> None:
        self._metrics = metrics
        self._crm_conversions = crm_conversions
        self._actionable_signals = actionable_signals
        self._recorder = recorder
        self._clock = clock

    async def execute(
        self, *, business_id: BusinessId, cutoff: datetime
    ) -> tuple[SignalContradicted, ...]:
        now = self._clock.now()
        refs = await self._actionable_signals.list_unresolved(
            business_id=business_id, cutoff=cutoff
        )
        contradicted: list[SignalContradicted] = []
        for ref in refs:
            reconciliation = await self._reconcile(business_id, ref)
            if not reconciliation.is_platform_claim_disproved:
                continue
            event = SignalContradicted(
                business_id=business_id,
                occurred_at=now,
                signal_id=ref.signal_id,
                entity_ref=ref.entity_ref,
                account_id=ref.account_id,
                rule_code=ref.rule_code,
                reconciliation=reconciliation,
            )
            await self._recorder.record(event=event)
            contradicted.append(event)
        return tuple(contradicted)

    async def _reconcile(
        self, business_id: BusinessId, ref: ActionableSignalRef
    ) -> ConversionReconciliation:
        facts = await self._metrics.find_in_window(
            entity_ref=ref.entity_ref, start_date=ref.window_start, end_date=ref.window_end
        )
        platform_conversions = sum(
            fact.conversions_of(ConversionKind.BUSINESS_CONVERSION) for fact in facts
        )
        crm_conversions = await self._crm_conversions.count_confirmed_conversions(
            business_id=business_id,
            entity_ref=ref.entity_ref,
            window_start=ref.window_start,
            window_end=ref.window_end,
        )
        return ConversionReconciliation(
            entity_ref=ref.entity_ref,
            window_start=ref.window_start,
            window_end=ref.window_end,
            platform_conversions=platform_conversions,
            crm_conversions=crm_conversions,
        )
