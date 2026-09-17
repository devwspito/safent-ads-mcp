"""`EvaluateSignalOutcomes` (profitability-engine.md §6, tasks.md T199):
paso de ciclo que resuelve el contraste a 14 dias de cada `Signal`
accionable vencida. Idempotente por construccion: `PendingSignalOutcomesPort
.list_due` solo devuelve senales sin fila en `signal_outcomes` todavia, asi
que reintentar el ciclo nunca duplica ni sobreescribe una ya resuelta."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from safent_ads.optimization.application.ports import (
    DueSignalOutcome,
    EntityCpaWindowPort,
    EntityCpaWindowSnapshot,
    PendingSignalOutcomesPort,
    RuleActionKindPort,
    SignalOutcomeRepository,
    SignalResolutionPort,
)
from safent_ads.optimization.domain.calibration import SignalOutcome, is_bad_entity, outcome_horizon
from safent_ads.optimization.domain.identifiers import SignalOutcomeId
from safent_ads.optimization.domain.signal_outcome_evaluation import resolve_correctness
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

_METRIC_MEASURES_BUSINESS_CONVERSION = False  # ver Assumption en el docstring de execute()


class EvaluateSignalOutcomes:
    """`metric_measures_business_conversion=False` para todo el catalogo
    hoy (ninguna clausula de `rules.yaml` mide conversion de negocio
    todavia): el horizonte queda fijo en 14 dias (`calibration.
    DEFAULT_OUTCOME_HORIZON_DAYS`). Cuando una regla mida conversion de
    negocio, este caso de uso necesitara `median_lag_days` por regla --
    fuera de esta lane (depende de `economics.LagCurve`, T157)."""

    def __init__(
        self,
        *,
        due_signals: PendingSignalOutcomesPort,
        resolution: SignalResolutionPort,
        cpa_window: EntityCpaWindowPort,
        action_kinds: RuleActionKindPort,
        outcomes: SignalOutcomeRepository,
        clock: Clock,
    ) -> None:
        self._due_signals = due_signals
        self._resolution = resolution
        self._cpa_window = cpa_window
        self._action_kinds = action_kinds
        self._outcomes = outcomes
        self._clock = clock

    async def execute(self, *, business_id: BusinessId) -> int:
        now = self._clock.now()
        horizon_days = outcome_horizon(
            metric_measures_business_conversion=_METRIC_MEASURES_BUSINESS_CONVERSION
        )
        cutoff = now - timedelta(days=horizon_days)
        due = await self._due_signals.list_due(business_id=business_id, cutoff=cutoff)
        for candidate in due:
            outcome = await self._evaluate_one(candidate, now=now, horizon_days=horizon_days)
            await self._outcomes.record(outcome=outcome)
        return len(due)

    async def _evaluate_one(
        self, candidate: DueSignalOutcome, *, now: datetime, horizon_days: int
    ) -> SignalOutcome:
        source = await self._resolution.resolve_source(signal_id=candidate.signal_id)
        action_kind = await self._action_kinds.get_action_kind(rule_code=candidate.rule_code)
        entity_is_bad = await self._entity_is_bad(candidate, as_of=now.date())
        was_correct = (
            None
            if action_kind is None
            else resolve_correctness(
                action_kind=action_kind, outcome_source=source, entity_is_bad=entity_is_bad
            )
        )
        return SignalOutcome(
            outcome_id=SignalOutcomeId.new(),
            business_id=str(candidate.business_id),
            account_id=candidate.account_id,
            rule_code=candidate.rule_code,
            signal_id=candidate.signal_id,
            outcome_source=source,
            was_correct=was_correct,
            observed_at=now,
            horizon_days=horizon_days,
        )

    async def _entity_is_bad(self, candidate: DueSignalOutcome, *, as_of: date) -> bool | None:
        snapshot = await self._cpa_window.get_snapshot(
            entity_ref=candidate.entity_ref,
            window_start=candidate.emitted_at.date(),
            window_end=as_of,
        )
        if snapshot is None:
            return None
        return _classify_bad(snapshot)


def _classify_bad(snapshot: EntityCpaWindowSnapshot) -> bool | None:
    if snapshot.target_cpa <= 0:
        return None
    if snapshot.leads == 0:
        # Gasto sin ni una conversion en toda la ventana es peor que
        # cualquier CPA finito; sin gasto tampoco, no hay dato que juzgar.
        return snapshot.spend > 0
    cpa = snapshot.spend / snapshot.leads
    return is_bad_entity(cpa=cpa, target_cpa=snapshot.target_cpa, window_days=snapshot.window_days)


__all__ = ["EvaluateSignalOutcomes"]
