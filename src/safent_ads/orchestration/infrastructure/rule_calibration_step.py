"""`LiveRuleCalibrationStep`: adaptador real de `RuleCalibrationStepPort`
(plan.md §7, profitability-engine.md §6, tasks.md T200). Una sesion propia
por vuelta, mismo patron que `LiveSignalOutcomeStep`."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.optimization.application.recalibrate_rules import RecalibrateRules
from safent_ads.optimization.infrastructure.sql_calibration_ports import (
    SqlCalibrationAdjustmentLogPort,
    SqlCalibrationInputPort,
    SqlRuleCalibrationStatePort,
)
from safent_ads.shared.clock import Clock


class LiveRuleCalibrationStep:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def run(self, cycle_id: str, now: datetime) -> None:
        del cycle_id, now
        async with self._session_factory() as session:
            await RecalibrateRules(
                inputs=SqlCalibrationInputPort(session),
                state=SqlRuleCalibrationStatePort(session),
                log=SqlCalibrationAdjustmentLogPort(session),
                clock=self._clock,
            ).execute()
            await session.commit()
