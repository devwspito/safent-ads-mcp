"""Adaptadores de los puertos de `metrics.application.ports` para T116
(`ReconcilePlatformVsCrm`), transversales por diseno -- igual que
`economics_step.py`, cruzan `crm`/`signals`/`optimization` sin que ninguno
de esos contextos se importe entre si.

`SqlCrmConversionsAdapter` envuelve `crm.infrastructure.sql_repositories.
SqlLeadAttributionRepository.count_by_kind_in_window` (ya existe desde
T145, no se duplica). `SqlSignalContradictionRecorderAdapter` marca
`signals.contradicted_at` (`signals.infrastructure.sql_repositories.
SqlSignalReconciliationRepository`, T116) Y alimenta el escritor YA
EXISTENTE de `optimization` (T199, `SqlSignalOutcomeRepository`/
`SqlSignalResolutionPort`) -- ninguno de los dos contextos se toca."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.crm.domain.conversion_kind import ConversionKind
from safent_ads.crm.infrastructure.sql_repositories import SqlLeadAttributionRepository
from safent_ads.metrics.domain.reconciliation import SignalContradicted
from safent_ads.optimization.domain.calibration import SignalOutcome
from safent_ads.optimization.domain.identifiers import SignalOutcomeId
from safent_ads.optimization.infrastructure.sql_signal_outcome_ports import (
    SqlSignalOutcomeRepository,
    SqlSignalResolutionPort,
)
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.signals.infrastructure.sql_repositories import SqlSignalReconciliationRepository


class SqlCrmConversionsAdapter:
    """`metrics.application.ports.CrmConversionsPort`: `window_end` que
    llega aqui es inclusivo (convencion de `metrics`); `count_by_kind_in_window`
    de `crm` es exclusivo, de ahi el `+1 dia`."""

    def __init__(self, session: AsyncSession) -> None:
        self._attributions = SqlLeadAttributionRepository(session)

    async def count_confirmed_conversions(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        window_start: date,
        window_end: date,
    ) -> int:
        return await self._attributions.count_by_kind_in_window(
            business_id=business_id,
            conversion_kind=ConversionKind.BUSINESS_CONVERSION,
            window_start=window_start,
            window_end=window_end + timedelta(days=1),
            entity_ref=str(entity_ref),
        )


class SqlSignalContradictionRecorderAdapter:
    """`metrics.application.ports.SignalContradictionRecorder`."""

    def __init__(self, session: AsyncSession) -> None:
        self._signals = SqlSignalReconciliationRepository(session)
        self._resolution = SqlSignalResolutionPort(session)
        self._outcomes = SqlSignalOutcomeRepository(session)

    async def record(self, *, event: SignalContradicted) -> None:
        await self._signals.mark_contradicted(
            signal_id=event.signal_id, contradicted_at=event.occurred_at
        )
        outcome_source = await self._resolution.resolve_source(signal_id=event.signal_id)
        await self._outcomes.record(
            outcome=SignalOutcome(
                outcome_id=SignalOutcomeId.new(),
                business_id=str(event.business_id),
                account_id=event.account_id,
                rule_code=event.rule_code,
                signal_id=event.signal_id,
                outcome_source=outcome_source,
                was_correct=False,
                observed_at=event.occurred_at,
            )
        )
