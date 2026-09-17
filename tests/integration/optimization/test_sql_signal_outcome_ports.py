"""Adaptadores SQL de T199 (profitability-engine.md §6) contra Postgres
real: `SqlPendingSignalOutcomesPort`, `SqlSignalResolutionPort`,
`SqlEntityCpaWindowPort`, `SqlRuleActionKindPort`, `SqlSignalOutcomeRepository`
-- y `EvaluateSignalOutcomes` de punta a punta con un reloj fijo."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.optimization.application.evaluate_signal_outcomes import EvaluateSignalOutcomes
from safent_ads.optimization.domain.calibration import OutcomeSource, SignalOutcome
from safent_ads.optimization.domain.identifiers import SignalOutcomeId
from safent_ads.optimization.infrastructure.sql_signal_outcome_ports import (
    SqlEntityCpaWindowPort,
    SqlPendingSignalOutcomesPort,
    SqlRuleActionKindPort,
    SqlSignalOutcomeRepository,
    SqlSignalResolutionPort,
)
from safent_ads.rules.domain.autonomy import ActionKind
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
_EMITTED_AT = _NOW - timedelta(days=20)  # ya vencio el horizonte de 14 dias

_INSERT_SIGNAL = text("""
    INSERT INTO signals (id, business_id, entity_ref, kind, strength, cause, cause_code,
                         rule_code, gate_verdicts, evidence, data_window, window_start,
                         window_end, money_at_stake_minor, money_at_stake_currency,
                         cycle_id, emitted_at)
    VALUES (:id, :business_id, :entity_ref, 'SELL', 70, 'CPA por encima del objetivo',
            'CPA_OVER_TARGET', :rule_code, '[]'::jsonb, '{}'::jsonb, '7D',
            :window_start, :window_end, 10000, 'EUR', :cycle_id, :emitted_at)
""")

_SET_BID_TARGET = text("""
    UPDATE ad_entities SET bid_target_amount_minor = :amount_minor, bid_target_currency = 'EUR'
     WHERE entity_ref = :entity_ref
""")

_INSERT_METRICS_DAY = text("""
    INSERT INTO metrics_daily (business_id, entity_ref, entity_level, platform_account_id,
                               stat_date, account_timezone, currency, spend, conversions_lead)
    VALUES (:business_id, :entity_ref, 'campaign', :platform_account_id, :stat_date,
            'Europe/Madrid', 'EUR', :spend, :leads)
""")

_CALIBRATE_RULE = text("""
    UPDATE rules
       SET condition = CAST(:condition AS jsonb), action = :action, magnitude_pct = 20,
           cooldown_minutes = 60, data_window = '7D', max_firings_per_day = 1,
           entity_level = 'campaign', autonomy_level = 'AUTO', is_enabled = true
     WHERE code = :code
""")

_CONDITION_JSON = (
    '{"clauses":[{"metric":"cpa_minor","comparator":"gt","window":"7d",'
    '"threshold_kind":"absolute","value":100}]}'
)

_INSERT_LINKED_PROPOSAL = text("""
    INSERT INTO proposals (business_id, entity_ref, parameter, current_value, proposed_value,
                           diff_hash, classification, cause_key, cause,
                           estimated_impact_amount, estimated_impact_currency, urgency,
                           signal_id, state, expires_at)
    VALUES (:business_id, :entity_ref, 'daily_budget', '100'::jsonb, '80'::jsonb,
            :diff_hash, 'routine', 'rule:M05', 'CPA por encima del objetivo',
            20, 'EUR', 'recommended', :signal_id, :state, :expires_at)
""")

_RULE_CODE = "M05"  # AUTO, SELL en rules.yaml (seed de 0007)


async def _seed_calibrated_entity(
    session: AsyncSession, *, entity_ref: EntityRef, bid_target_minor: int
) -> uuid.UUID:
    business_id = await seed_entity(session, entity_ref)
    await session.execute(
        _SET_BID_TARGET, {"entity_ref": str(entity_ref), "amount_minor": bid_target_minor}
    )
    await session.execute(
        _CALIBRATE_RULE, {"code": _RULE_CODE, "condition": _CONDITION_JSON, "action": "SELL"}
    )
    await session.flush()
    return business_id


async def _insert_signal(
    session: AsyncSession, *, business_id: uuid.UUID, entity_ref: EntityRef, signal_id: uuid.UUID
) -> None:
    await session.execute(
        _INSERT_SIGNAL,
        {
            "id": signal_id,
            "business_id": business_id,
            "entity_ref": str(entity_ref),
            "rule_code": _RULE_CODE,
            "window_start": _EMITTED_AT.date() - timedelta(days=7),
            "window_end": _EMITTED_AT.date(),
            "cycle_id": uuid.uuid4(),
            "emitted_at": _EMITTED_AT,
        },
    )
    await session.flush()


class TestSqlPendingSignalOutcomesPort:
    async def test_lists_only_signals_past_the_cutoff_without_an_outcome_yet(
        self, db_session: AsyncSession
    ) -> None:
        entity_ref = campaign_ref(f"due-{id(db_session)}")
        business_id = await _seed_calibrated_entity(
            db_session, entity_ref=entity_ref, bid_target_minor=1000
        )
        signal_id = uuid.uuid4()
        await _insert_signal(
            db_session, business_id=business_id, entity_ref=entity_ref, signal_id=signal_id
        )

        due = await SqlPendingSignalOutcomesPort(db_session).list_due(
            business_id=BusinessId(business_id), cutoff=_NOW - timedelta(days=14)
        )

        assert len(due) == 1
        assert due[0].signal_id == str(signal_id)
        assert due[0].rule_code == _RULE_CODE
        assert due[0].entity_ref == entity_ref

    async def test_excludes_signals_already_resolved(self, db_session: AsyncSession) -> None:
        entity_ref = campaign_ref(f"resolved-{id(db_session)}")
        business_id = await _seed_calibrated_entity(
            db_session, entity_ref=entity_ref, bid_target_minor=1000
        )
        signal_id = uuid.uuid4()
        await _insert_signal(
            db_session, business_id=business_id, entity_ref=entity_ref, signal_id=signal_id
        )
        account_row = (
            await db_session.execute(
                text("SELECT platform_account_id FROM ad_entities WHERE entity_ref = :ref"),
                {"ref": str(entity_ref)},
            )
        ).one()
        outcomes = SqlSignalOutcomeRepository(db_session)
        await outcomes.record(
            outcome=SignalOutcome(
                outcome_id=SignalOutcomeId.new(),
                business_id=str(business_id),
                account_id=str(account_row.platform_account_id),
                rule_code=_RULE_CODE,
                signal_id=str(signal_id),
                outcome_source=OutcomeSource.EXPIRED,
                was_correct=True,
                observed_at=_NOW,
            )
        )

        due = await SqlPendingSignalOutcomesPort(db_session).list_due(
            business_id=BusinessId(business_id), cutoff=_NOW - timedelta(days=14)
        )

        assert due == ()


class TestSqlSignalResolutionPort:
    async def test_defaults_to_expired_without_a_linked_proposal(
        self, db_session: AsyncSession
    ) -> None:
        source = await SqlSignalResolutionPort(db_session).resolve_source(
            signal_id=str(uuid.uuid4())
        )
        assert source is OutcomeSource.EXPIRED

    async def test_resolves_applied_from_an_executed_linked_proposal(
        self, db_session: AsyncSession
    ) -> None:
        entity_ref = campaign_ref(f"applied-{id(db_session)}")
        business_id = await seed_entity(db_session, entity_ref)
        signal_id = uuid.uuid4()
        await _insert_signal(
            db_session, business_id=business_id, entity_ref=entity_ref, signal_id=signal_id
        )
        await db_session.execute(
            _INSERT_LINKED_PROPOSAL,
            {
                "business_id": business_id,
                "entity_ref": str(entity_ref),
                "diff_hash": "a" * 64,
                "signal_id": signal_id,
                "state": "executed",
                "expires_at": _NOW + timedelta(days=1),
            },
        )

        source = await SqlSignalResolutionPort(db_session).resolve_source(
            signal_id=str(signal_id)
        )

        assert source is OutcomeSource.APPLIED


class TestSqlEntityCpaWindowPort:
    async def test_returns_none_without_a_reliable_bid_target(
        self, db_session: AsyncSession
    ) -> None:
        entity_ref = campaign_ref(f"notarget-{id(db_session)}")
        await seed_entity(db_session, entity_ref)

        snapshot = await SqlEntityCpaWindowPort(db_session).get_snapshot(
            entity_ref=entity_ref,
            window_start=_EMITTED_AT.date(),
            window_end=_NOW.date(),
        )

        assert snapshot is None

    async def test_aggregates_spend_and_leads_over_the_window(
        self, db_session: AsyncSession
    ) -> None:
        entity_ref = campaign_ref(f"cpa-{id(db_session)}")
        business_id = await _seed_calibrated_entity(
            db_session, entity_ref=entity_ref, bid_target_minor=1000
        )
        account_row = (
            await db_session.execute(
                text("SELECT platform_account_id FROM ad_entities WHERE entity_ref = :ref"),
                {"ref": str(entity_ref)},
            )
        ).one()
        for offset, (spend, leads) in enumerate([("50", 1), ("30", 0)]):
            await db_session.execute(
                _INSERT_METRICS_DAY,
                {
                    "business_id": business_id,
                    "entity_ref": str(entity_ref),
                    "platform_account_id": account_row.platform_account_id,
                    "stat_date": _EMITTED_AT.date() + timedelta(days=offset),
                    "spend": spend,
                    "leads": leads,
                },
            )

        snapshot = await SqlEntityCpaWindowPort(db_session).get_snapshot(
            entity_ref=entity_ref,
            window_start=_EMITTED_AT.date(),
            window_end=_EMITTED_AT.date() + timedelta(days=1),
        )

        assert snapshot is not None
        assert snapshot.spend == pytest.approx(80.0)
        assert snapshot.leads == 1
        assert snapshot.target_cpa == pytest.approx(10.0)


class TestSqlRuleActionKindPort:
    async def test_returns_the_calibrated_action_kind(self, db_session: AsyncSession) -> None:
        entity_ref = campaign_ref(f"action-{id(db_session)}")
        await _seed_calibrated_entity(db_session, entity_ref=entity_ref, bid_target_minor=1000)

        action_kind = await SqlRuleActionKindPort(db_session).get_action_kind(
            rule_code=_RULE_CODE
        )

        assert action_kind is ActionKind.SELL

    async def test_returns_none_for_an_unknown_code(self, db_session: AsyncSession) -> None:
        action_kind = await SqlRuleActionKindPort(db_session).get_action_kind(
            rule_code="Z99"
        )
        assert action_kind is None


class TestEvaluateSignalOutcomesEndToEnd:
    async def test_evaluates_a_due_signal_with_a_fixed_clock(
        self, db_session: AsyncSession
    ) -> None:
        entity_ref = campaign_ref(f"e2e-{id(db_session)}")
        business_id = await _seed_calibrated_entity(
            db_session, entity_ref=entity_ref, bid_target_minor=1000
        )
        signal_id = uuid.uuid4()
        await _insert_signal(
            db_session, business_id=business_id, entity_ref=entity_ref, signal_id=signal_id
        )
        account_row = (
            await db_session.execute(
                text("SELECT platform_account_id FROM ad_entities WHERE entity_ref = :ref"),
                {"ref": str(entity_ref)},
            )
        ).one()
        await db_session.execute(
            _INSERT_METRICS_DAY,
            {
                "business_id": business_id,
                "entity_ref": str(entity_ref),
                "platform_account_id": account_row.platform_account_id,
                "stat_date": _EMITTED_AT.date(),
                "spend": "100",
                "leads": 1,
            },
        )

        use_case = EvaluateSignalOutcomes(
            due_signals=SqlPendingSignalOutcomesPort(db_session),
            resolution=SqlSignalResolutionPort(db_session),
            cpa_window=SqlEntityCpaWindowPort(db_session),
            action_kinds=SqlRuleActionKindPort(db_session),
            outcomes=SqlSignalOutcomeRepository(db_session),
            clock=FixedClock(_NOW),
        )

        evaluated = await use_case.execute(business_id=BusinessId(business_id))

        assert evaluated == 1
        row = (
            await db_session.execute(
                text(
                    "SELECT outcome_source, was_correct FROM signal_outcomes "
                    "WHERE signal_id = :signal_id"
                ),
                {"signal_id": signal_id},
            )
        ).one()
        assert row.outcome_source == "expired"
        assert row.was_correct is True  # CPA 100/1 = 100 > 1.5*10: entidad mala, control confirma

        signal_row = (
            await db_session.execute(
                text("SELECT outcome_at_14d FROM signals WHERE id = :id"), {"id": signal_id}
            )
        ).one()
        assert signal_row.outcome_at_14d == {"status": "confirmed"}

        # Idempotente: una segunda vuelta del ciclo no vuelve a evaluar la misma senal.
        evaluated_again = await use_case.execute(business_id=BusinessId(business_id))
        assert evaluated_again == 0
