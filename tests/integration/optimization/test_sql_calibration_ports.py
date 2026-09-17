"""Adaptadores SQL de T200 (profitability-engine.md §6) contra Postgres
real: `SqlCalibrationInputPort`, `SqlRuleCalibrationStatePort`,
`SqlCalibrationAdjustmentLogPort` -- y `RecalibrateRules` de punta a punta,
incluida la entrada real en `decision_log`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.optimization.application.ports import RuleCalibrationState
from safent_ads.optimization.application.recalibrate_rules import RecalibrateRules
from safent_ads.optimization.domain.calibration import AutonomyLevel
from safent_ads.optimization.infrastructure.sql_calibration_ports import (
    SqlCalibrationAdjustmentLogPort,
    SqlCalibrationInputPort,
    SqlRuleCalibrationStatePort,
)
from safent_ads.shared.clock import FixedClock
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_RULE_CODE = "G01"

_CONDITION_JSON = (
    '{"clauses":[{"metric":"cpa_minor","comparator":"gt","window":"7d",'
    '"threshold_kind":"absolute","value":100}]}'
)

_CALIBRATE_RULE = text("""
    UPDATE rules
       SET action = 'SELL', magnitude_pct = :magnitude_pct, cooldown_minutes = 60,
           data_window = '7D', max_firings_per_day = 1, entity_level = 'campaign',
           condition = CAST(:condition AS jsonb),
           autonomy_level = 'AUTO', is_enabled = true
     WHERE code = :code
""")

_INSERT_SIGNAL = text("""
    INSERT INTO signals (id, business_id, entity_ref, kind, strength, cause, cause_code,
                         rule_code, gate_verdicts, evidence, data_window, window_start,
                         window_end, money_at_stake_minor, money_at_stake_currency,
                         cycle_id, emitted_at)
    VALUES (:id, :business_id, :entity_ref, 'SELL', 70, 'CPA por encima del objetivo',
            'CPA_OVER_TARGET', :rule_code, '[]'::jsonb, '{}'::jsonb, '7D',
            :window_start, :window_end, 10000, 'EUR', :cycle_id, :emitted_at)
""")

_INSERT_OUTCOME = text("""
    INSERT INTO signal_outcomes (id, business_id, account_id, rule_code, signal_id,
                                 outcome_source, was_correct, horizon_days, observed_at)
    VALUES (gen_random_uuid(), :business_id, :account_id, :rule_code, :signal_id,
            'expired', :was_correct, 14, :observed_at)
""")


async def _seed_outcomes(
    session: AsyncSession, *, business_id: uuid.UUID, account_id: uuid.UUID, n: int, correct: int
) -> None:
    entity_ref = (
        await session.execute(
            text("SELECT entity_ref FROM ad_entities WHERE business_id = :business_id LIMIT 1"),
            {"business_id": business_id},
        )
    ).scalar_one()
    for i in range(n):
        signal_id = uuid.uuid4()
        await session.execute(
            _INSERT_SIGNAL,
            {
                "id": signal_id,
                "business_id": business_id,
                "entity_ref": str(entity_ref),
                "rule_code": _RULE_CODE,
                "window_start": _NOW.date() - timedelta(days=21),
                "window_end": _NOW.date() - timedelta(days=14),
                "cycle_id": uuid.uuid4(),
                "emitted_at": _NOW - timedelta(days=20),
            },
        )
        await session.execute(
            _INSERT_OUTCOME,
            {
                "business_id": business_id,
                "account_id": account_id,
                "rule_code": _RULE_CODE,
                "signal_id": signal_id,
                "was_correct": i < correct,
                "observed_at": _NOW,
            },
        )
    await session.flush()


async def _account_id(session: AsyncSession, business_id: uuid.UUID) -> uuid.UUID:
    row = (
        await session.execute(
            text("SELECT id FROM platform_accounts WHERE business_id = :business_id LIMIT 1"),
            {"business_id": business_id},
        )
    ).one()
    return row.id


class TestSqlCalibrationInputPort:
    async def test_lists_rule_codes_and_their_outcomes(self, db_session: AsyncSession) -> None:
        business_id = await seed_entity(db_session, campaign_ref(f"cal-in-{id(db_session)}"))
        await db_session.execute(
            _CALIBRATE_RULE,
            {"code": _RULE_CODE, "magnitude_pct": 30, "condition": _CONDITION_JSON},
        )
        account_id = await _account_id(db_session, business_id)
        await _seed_outcomes(
            db_session, business_id=business_id, account_id=account_id, n=5, correct=2
        )

        port = SqlCalibrationInputPort(db_session)
        codes = await port.list_rule_codes_with_outcomes()
        outcomes = await port.list_outcomes_for_rule(rule_code=_RULE_CODE)

        assert _RULE_CODE in codes
        assert len(outcomes) == 5
        assert sum(1 for o in outcomes if o.was_correct) == 2


class TestSqlRuleCalibrationStatePort:
    async def test_reads_and_applies_a_magnitude_pct_adjustment(
        self, db_session: AsyncSession
    ) -> None:
        business_id = await seed_entity(db_session, campaign_ref(f"cal-state-{id(db_session)}"))
        await db_session.execute(
            _CALIBRATE_RULE,
            {"code": _RULE_CODE, "magnitude_pct": 30, "condition": _CONDITION_JSON},
        )
        del business_id

        port = SqlRuleCalibrationStatePort(db_session)
        state = await port.get_state(rule_code=_RULE_CODE)
        assert state == RuleCalibrationState(
            rule_code=_RULE_CODE,
            autonomy_level=AutonomyLevel.AUTO,
            magnitude_pct=30.0,
            cooldown_minutes=60.0,
        )

        await port.apply_adjustment(
            rule_code=_RULE_CODE, threshold_name="magnitude_pct", new_value=25.0
        )

        updated = await port.get_state(rule_code=_RULE_CODE)
        assert updated is not None
        assert updated.magnitude_pct == pytest.approx(25.0)


class TestEvaluateRecalibrateRulesEndToEnd:
    async def test_tightens_and_logs_a_decision_per_contributing_business(
        self, db_session: AsyncSession
    ) -> None:
        business_a = await seed_entity(db_session, campaign_ref(f"cal-e2e-a-{id(db_session)}"))
        business_b = await seed_entity(db_session, campaign_ref(f"cal-e2e-b-{id(db_session)}"))
        await db_session.execute(
            _CALIBRATE_RULE,
            {"code": _RULE_CODE, "magnitude_pct": 30, "condition": _CONDITION_JSON},
        )
        account_a = await _account_id(db_session, business_a)
        account_b = await _account_id(db_session, business_b)
        await _seed_outcomes(
            db_session, business_id=business_a, account_id=account_a, n=15, correct=3
        )
        await _seed_outcomes(
            db_session, business_id=business_b, account_id=account_b, n=15, correct=3
        )

        use_case = RecalibrateRules(
            inputs=SqlCalibrationInputPort(db_session),
            state=SqlRuleCalibrationStatePort(db_session),
            log=SqlCalibrationAdjustmentLogPort(db_session),
            clock=FixedClock(_NOW),
        )

        adjustments = await use_case.execute()

        assert len(adjustments) == 1
        assert adjustments[0].new_value < 30.0
        rule_row = (
            await db_session.execute(
                text("SELECT magnitude_pct FROM rules WHERE code = :code"), {"code": _RULE_CODE}
            )
        ).one()
        assert float(rule_row.magnitude_pct) == pytest.approx(adjustments[0].new_value)
        decision_rows = (
            await db_session.execute(
                text(
                    "SELECT business_id FROM decision_log WHERE event_type = 'rule_change' "
                    "AND payload->>'rule_code' = :code"
                ),
                {"code": _RULE_CODE},
            )
        ).all()
        assert {row.business_id for row in decision_rows} == {business_a, business_b}

        # Idempotente en la semana: una segunda vuelta no vuelve a ajustar.
        second_pass = await use_case.execute()
        assert second_pass == ()
