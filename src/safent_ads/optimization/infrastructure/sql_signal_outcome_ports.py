"""Adaptadores SQL de T199 (profitability-engine.md §6) sobre `signals`,
`ad_entities`, `metrics_daily`, `proposals`, `rules` y la tabla nueva
`signal_outcomes` (0026_experiments).

`orchestration.infrastructure.rule_step._resolve_firing` ya rellena
`Cause(signal_id=...)` con el `id` real de la `Signal` que disparo la
regla, asi que `proposals.signal_id` enlaza toda propuesta nacida de una
regla con su senal de origen. `SqlSignalResolutionPort.resolve_source`
sigue devolviendo `EXPIRED` para el resto de casos genuinos ("sin
propuesta enlazada", propuesta `postponed`/`invalidated`/... todavia sin
desenlace): lectura honesta ("la ventana se cerro sin una decision que
contrastar"), no un `applied` inventado, y coincide con el grupo de
control gratis que pide §6."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.optimization.application.ports import DueSignalOutcome, EntityCpaWindowSnapshot
from safent_ads.optimization.domain.calibration import OutcomeSource, SignalOutcome
from safent_ads.rules.domain.autonomy import ActionKind
from safent_ads.rules.infrastructure.sql_repositories import SqlRuleRepository
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = [
    "SqlEntityCpaWindowPort",
    "SqlPendingSignalOutcomesPort",
    "SqlRuleActionKindPort",
    "SqlSignalOutcomeRepository",
    "SqlSignalResolutionPort",
]

_LIST_DUE: Final = """
    SELECT s.id AS signal_id, s.entity_ref, s.rule_code, s.emitted_at,
           e.platform_account_id AS account_id
      FROM signals AS s
      JOIN ad_entities AS e
        ON e.business_id = s.business_id AND e.entity_ref = s.entity_ref
      LEFT JOIN signal_outcomes AS so ON so.signal_id = s.id
     WHERE s.business_id = :business_id
       AND s.kind <> 'HOLD'
       AND s.emitted_at <= :cutoff
       AND so.signal_id IS NULL
     ORDER BY s.emitted_at
"""


class SqlPendingSignalOutcomesPort:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_due(
        self, *, business_id: BusinessId, cutoff: datetime
    ) -> tuple[DueSignalOutcome, ...]:
        rows = (
            await self._session.execute(
                text(_LIST_DUE), {"business_id": business_id.value, "cutoff": cutoff}
            )
        ).mappings()
        return tuple(
            DueSignalOutcome(
                signal_id=str(row["signal_id"]),
                business_id=business_id,
                account_id=str(row["account_id"]),
                entity_ref=EntityRef.parse(row["entity_ref"]),
                rule_code=row["rule_code"],
                emitted_at=row["emitted_at"],
            )
            for row in rows
        )


_LATEST_PROPOSAL_STATE: Final = """
    SELECT state FROM proposals WHERE signal_id = :signal_id ORDER BY created_at DESC LIMIT 1
"""

_OUTCOME_SOURCE_BY_PROPOSAL_STATE: Final[dict[str, OutcomeSource]] = {
    "executed": OutcomeSource.APPLIED,
    "rejected": OutcomeSource.REJECTED,
}


class SqlSignalResolutionPort:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_source(self, *, signal_id: str) -> OutcomeSource:
        row = (
            await self._session.execute(text(_LATEST_PROPOSAL_STATE), {"signal_id": signal_id})
        ).mappings().first()
        if row is None:
            return OutcomeSource.EXPIRED
        return _OUTCOME_SOURCE_BY_PROPOSAL_STATE.get(row["state"], OutcomeSource.EXPIRED)


_WINDOW_METRICS: Final = """
    SELECT coalesce(sum(spend), 0) AS spend, coalesce(sum(conversions_lead), 0) AS leads
      FROM metrics_daily
     WHERE entity_ref = :entity_ref AND stat_date BETWEEN :window_start AND :window_end
"""

_ENTITY_TARGET: Final = """
    SELECT e.bid_target_amount_minor, e.bid_target_currency, a.currency AS account_currency
      FROM ad_entities AS e
      JOIN platform_accounts AS a ON a.id = e.platform_account_id
     WHERE e.entity_ref = :entity_ref
"""

_MINOR_UNITS_PER_MAJOR: Final = 100


class SqlEntityCpaWindowPort:
    """`target_cpa` viene de `ad_entities.bid_target` -- la misma fuente
    que `LiveSignalStep` usa para disparar la senal (unica real hoy, ver
    su docstring); divisa distinta a la de la cuenta se trata como "sin
    objetivo fiable", nunca se compara a ciegas."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_snapshot(
        self, *, entity_ref: EntityRef, window_start: date, window_end: date
    ) -> EntityCpaWindowSnapshot | None:
        target = (
            await self._session.execute(text(_ENTITY_TARGET), {"entity_ref": str(entity_ref)})
        ).mappings().first()
        if target is None or not _has_reliable_target(target):
            return None
        metrics = (
            await self._session.execute(
                text(_WINDOW_METRICS),
                {
                    "entity_ref": str(entity_ref),
                    "window_start": window_start,
                    "window_end": window_end,
                },
            )
        ).mappings().one()
        return EntityCpaWindowSnapshot(
            spend=float(metrics["spend"]),
            leads=int(metrics["leads"]),
            target_cpa=float(target["bid_target_amount_minor"]) / _MINOR_UNITS_PER_MAJOR,
            window_days=(window_end - window_start).days,
        )


def _has_reliable_target(target: RowMapping) -> bool:
    amount = target["bid_target_amount_minor"]
    currency = target["bid_target_currency"]
    account_currency = target["account_currency"]
    return amount is not None and currency is not None and currency == account_currency


class SqlRuleActionKindPort:
    def __init__(self, session: AsyncSession) -> None:
        self._rules = SqlRuleRepository(session)

    async def get_action_kind(self, *, rule_code: str) -> ActionKind | None:
        stored = await self._rules.get_by_code(rule_code)
        return None if stored is None else stored.rule.action_kind


_INSERT_SIGNAL_OUTCOME: Final = """
    INSERT INTO signal_outcomes (id, business_id, account_id, rule_code, signal_id,
                                 outcome_source, was_correct, horizon_days, observed_at)
    VALUES (gen_random_uuid(), :business_id, :account_id, :rule_code, :signal_id,
            :outcome_source, :was_correct, :horizon_days, :observed_at)
    ON CONFLICT (signal_id) DO NOTHING
"""

_MARK_SIGNAL_RESOLVED: Final = """
    UPDATE signals SET outcome_at_14d = CAST(:status_json AS jsonb), outcome_evaluated_at = :now
     WHERE id = :signal_id AND outcome_at_14d IS NULL
"""


class SqlSignalOutcomeRepository:
    """Escribe la fila canonica en `signal_outcomes` siempre; solo
    materializa `signals.outcome_at_14d` (lo que ya leen panel/mcp) cuando
    `was_correct` no es `None` -- nunca un `confirmed`/`not_confirmed`
    inventado para una senal inconcluyente (mismo criterio que el
    docstring de `panel.infrastructure.sql_read_model`)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, *, outcome: SignalOutcome) -> None:
        await self._session.execute(
            text(_INSERT_SIGNAL_OUTCOME),
            {
                "business_id": outcome.business_id,
                "account_id": outcome.account_id,
                "rule_code": outcome.rule_code,
                "signal_id": outcome.signal_id,
                "outcome_source": outcome.outcome_source.value,
                "was_correct": outcome.was_correct,
                "horizon_days": outcome.horizon_days,
                "observed_at": outcome.observed_at,
            },
        )
        if outcome.was_correct is not None:
            await self._mark_signal_resolved(outcome)
        await self._session.flush()

    async def _mark_signal_resolved(self, outcome: SignalOutcome) -> None:
        status = "confirmed" if outcome.was_correct else "not_confirmed"
        await self._session.execute(
            text(_MARK_SIGNAL_RESOLVED),
            {
                "signal_id": outcome.signal_id,
                "status_json": json.dumps({"status": status}),
                "now": outcome.observed_at,
            },
        )
