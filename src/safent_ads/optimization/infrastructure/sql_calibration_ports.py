"""Adaptadores SQL de T200 (profitability-engine.md §6) sobre
`signal_outcomes`, `rules` y `calibration_adjustments`/`audit.decision_log`
(0026_experiments, 0007_rules_guardrails, 0002_audit_chain).

`SqlRuleCalibrationStatePort.apply_adjustment` escribe la MISMA columna que
lee `rules.infrastructure.sql_repositories._to_stored_rule` (`magnitude_pct`
/`cooldown_minutes`): el ajuste cambia el comportamiento en vivo del motor
de reglas en el siguiente ciclo, no una copia de exhibicion."""

from __future__ import annotations

from datetime import date
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.audit.application.ports import DecisionLogRepository
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.optimization.application.ports import RuleCalibrationState
from safent_ads.optimization.domain.calibration import (
    AutonomyLevel,
    CalibrationAdjustment,
    OutcomeSource,
    SignalOutcome,
)
from safent_ads.optimization.domain.identifiers import SignalOutcomeId
from safent_ads.shared.ids import BusinessId

__all__ = [
    "SqlCalibrationAdjustmentLogPort",
    "SqlCalibrationInputPort",
    "SqlRuleCalibrationStatePort",
]

_LIST_RULE_CODES_WITH_OUTCOMES: Final = "SELECT DISTINCT rule_code FROM signal_outcomes"

_LIST_OUTCOMES_FOR_RULE: Final = """
    SELECT business_id, account_id, rule_code, signal_id, outcome_source, was_correct,
           horizon_days, observed_at
      FROM signal_outcomes
     WHERE rule_code = :rule_code
"""


class SqlCalibrationInputPort:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_rule_codes_with_outcomes(self) -> tuple[str, ...]:
        rows = (await self._session.execute(text(_LIST_RULE_CODES_WITH_OUTCOMES))).mappings()
        return tuple(row["rule_code"] for row in rows)

    async def list_outcomes_for_rule(self, *, rule_code: str) -> tuple[SignalOutcome, ...]:
        rows = (
            await self._session.execute(
                text(_LIST_OUTCOMES_FOR_RULE), {"rule_code": rule_code}
            )
        ).mappings()
        return tuple(
            SignalOutcome(
                outcome_id=SignalOutcomeId.new(),
                business_id=str(row["business_id"]),
                account_id=str(row["account_id"]),
                rule_code=row["rule_code"],
                signal_id=str(row["signal_id"]),
                outcome_source=OutcomeSource(row["outcome_source"]),
                was_correct=row["was_correct"],
                observed_at=row["observed_at"],
                horizon_days=row["horizon_days"],
            )
            for row in rows
        )


_SELECT_RULE_STATE: Final = """
    SELECT code, autonomy_level, magnitude_pct, cooldown_minutes
      FROM rules
     WHERE code = :rule_code AND scope = 'global'
"""

_UPDATE_MAGNITUDE_PCT: Final = "UPDATE rules SET magnitude_pct = :new_value WHERE code = :rule_code"
_UPDATE_COOLDOWN_MINUTES: Final = (
    "UPDATE rules SET cooldown_minutes = :new_value WHERE code = :rule_code"
)


class SqlRuleCalibrationStatePort:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_state(self, *, rule_code: str) -> RuleCalibrationState | None:
        row = (
            await self._session.execute(text(_SELECT_RULE_STATE), {"rule_code": rule_code})
        ).mappings().first()
        if row is None or row["cooldown_minutes"] is None:
            return None
        return RuleCalibrationState(
            rule_code=row["code"],
            autonomy_level=AutonomyLevel(row["autonomy_level"].lower()),
            magnitude_pct=(
                float(row["magnitude_pct"]) if row["magnitude_pct"] is not None else None
            ),
            cooldown_minutes=float(row["cooldown_minutes"]),
        )

    async def apply_adjustment(
        self, *, rule_code: str, threshold_name: str, new_value: float
    ) -> None:
        query = (
            _UPDATE_MAGNITUDE_PCT
            if threshold_name == "magnitude_pct"
            else _UPDATE_COOLDOWN_MINUTES
        )
        await self._session.execute(text(query), {"rule_code": rule_code, "new_value": new_value})
        await self._session.flush()


_ALREADY_ADJUSTED_THIS_WEEK: Final = """
    SELECT 1 FROM calibration_adjustments
     WHERE rule_code = :rule_code AND threshold_name = :threshold_name AND week_start = :week_start
"""

_INSERT_CALIBRATION_ADJUSTMENT: Final = """
    INSERT INTO calibration_adjustments (id, rule_code, threshold_name, direction,
                                         previous_value, new_value, autonomy_level,
                                         sample_size, precision_value, week_start)
    VALUES (:id, :rule_code, :threshold_name, :direction, :previous_value, :new_value,
            :autonomy_level, :sample_size, :precision_value, :week_start)
    ON CONFLICT ON CONSTRAINT calibration_adjustments_weekly_unique DO NOTHING
"""


class SqlCalibrationAdjustmentLogPort:
    """`record_adjustment` escribe la bitacora canonica (`calibration_
    adjustments`) y una entrada de `decision_log` POR NEGOCIO que aporto
    contraste -- `decision_log.business_id` es NOT NULL y `rules` es
    catalogo global (ver Assumption de `RecalibrateRules`): no hay un
    unico negocio dueno del ajuste, asi que cada uno que participo se
    entera por su propia fila."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._decisions = RecordDecision(_decision_log_repository(session))

    async def already_adjusted_this_week(
        self, *, rule_code: str, threshold_name: str, week_start: date
    ) -> bool:
        row = (
            await self._session.execute(
                text(_ALREADY_ADJUSTED_THIS_WEEK),
                {
                    "rule_code": rule_code,
                    "threshold_name": threshold_name,
                    "week_start": week_start,
                },
            )
        ).first()
        return row is not None

    async def record_adjustment(
        self,
        *,
        adjustment: CalibrationAdjustment,
        sample_size: int,
        precision: float | None,
        week_start: date,
        business_ids: tuple[str, ...],
    ) -> None:
        await self._session.execute(
            text(_INSERT_CALIBRATION_ADJUSTMENT),
            {
                "id": adjustment.adjustment_id.value,
                "rule_code": adjustment.rule_code,
                "threshold_name": adjustment.threshold_name,
                "direction": adjustment.direction.value,
                "previous_value": adjustment.previous_value,
                "new_value": adjustment.new_value,
                "autonomy_level": adjustment.autonomy_level.value,
                "sample_size": sample_size,
                "precision_value": precision,
                "week_start": week_start,
            },
        )
        for business_id in business_ids:
            await self._log_decision(adjustment, business_id=business_id, precision=precision)
        await self._session.flush()

    async def _log_decision(
        self, adjustment: CalibrationAdjustment, *, business_id: str, precision: float | None
    ) -> None:
        await self._decisions.execute(
            PendingDecision(
                business_id=BusinessId.parse(business_id),
                kind=DecisionKind.RULE_CHANGE,
                actor_kind=ActorKind.SYSTEM,
                payload={
                    "rule_code": adjustment.rule_code,
                    "threshold_name": adjustment.threshold_name,
                    "direction": adjustment.direction.value,
                    "previous_value": adjustment.previous_value,
                    "new_value": adjustment.new_value,
                    "precision": precision,
                },
            )
        )


def _decision_log_repository(session: AsyncSession) -> DecisionLogRepository:
    return SqlDecisionLogRepository(session)
