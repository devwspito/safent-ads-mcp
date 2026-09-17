"""`RuleCycle` -> `ExecutionCycle` de extremo a extremo (plan.md §7, T068)
contra Postgres real: senal SELL viva sobre una regla `AUTO` -> propuesta
creada y autorizada -> encolada -> `ExecutionCycle` la reclama y llega
hasta el bróker (`NO_WRITE_PATH_IN_F1`, `PlatformWriteDeniedError` typed).

Mismo cuidado de aislamiento que `tests/integration/composition/
test_write_path_end_to_end.py`: `rolled_back_session` para el montaje
(`RuleCycle` confirma su propia transaccion a mitad de camino, igual que
el chokepoint) y `ADS_BROKER_SOCKET` apuntando a un socket que no existe."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.composition.container import Container
from safent_ads.execution.domain.guardrails import GuardrailScope, ScopeKind
from safent_ads.optimization.application.evaluate_signal_outcomes import EvaluateSignalOutcomes
from safent_ads.optimization.infrastructure.sql_signal_outcome_ports import (
    SqlEntityCpaWindowPort,
    SqlPendingSignalOutcomesPort,
    SqlRuleActionKindPort,
    SqlSignalOutcomeRepository,
    SqlSignalResolutionPort,
)
from safent_ads.orchestration.infrastructure.rule_step import LiveRuleStep
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.signals.domain.cause import Cause
from safent_ads.signals.domain.signal import Signal, SignalKind
from safent_ads.signals.domain.value_objects import Evidence as SignalEvidence
from safent_ads.signals.domain.value_objects import MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.infrastructure.sql_repositories import SqlSignalRepository
from tests.conftest import rolled_back_session
from tests.contracts.execution.conftest import (
    NOW,
    GuardrailLimits,
    calibrate_rule,
    seed_freshness,
    seed_guardrails,
    seed_sell_signal,
)
from tests.contracts.sql_fixtures import campaign_ref, seed_entity
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_SET_BUDGET = text(
    "UPDATE ad_entities SET budget_amount_minor = 10000, budget_currency = 'EUR', "
    "budget_kind = 'daily' WHERE entity_ref = :entity_ref"
)

_BUY_RULE_CODE = "M01"

# Toda regla `BUY` es `approval` por invariante de dominio (rules/domain/
# rule.py, FR-11/FR-12): nunca `AUTO`. `evaluate_rule` no reevalua esta
# condicion JSON en tiempo de disparo (solo compara `rule.code` == `signal.
# rule_code` y `rule.action_kind` -> `signal.kind`, ver `rule_evaluator.py`)
# -- cualquier JSON valido basta.
_BUY_CONDITION = json.dumps(
    {
        "clauses": [
            {
                "metric": "roas",
                "comparator": "gt",
                "window": "t",
                "threshold_kind": "absolute",
                "value": 1.2,
            }
        ]
    }
)

_CALIBRATE_BUY_RULE = text("""
    UPDATE rules
       SET is_enabled = true, autonomy_level = 'APPROVAL', action = 'BUY',
           entity_level = 'campaign', data_window = '7D', cooldown_minutes = 60,
           max_firings_per_day = 3, magnitude_pct = 30,
           condition = CAST(:condition AS jsonb)
     WHERE code = :code AND scope = 'global'
""")


async def _calibrate_buy_rule(session: AsyncSession) -> None:
    await session.execute(
        _CALIBRATE_BUY_RULE, {"code": _BUY_RULE_CODE, "condition": _BUY_CONDITION}
    )
    await session.flush()


async def _seed_buy_signal(session: AsyncSession, entity_ref: EntityRef) -> None:
    await SqlSignalRepository(session, cycle_id=uuid.uuid4()).save(
        Signal(
            entity_ref=entity_ref,
            kind=SignalKind.BUY,
            strength=SignalStrength(70),
            cause=Cause.LIMITED_BY_BUDGET_AT_TARGET,
            cause_sentence="ROAS por encima del objetivo, limitado por presupuesto",
            span=WindowSpan.D7,
            money_at_stake=MoneyAtStake(minor_units=12_000, currency="EUR"),
            evidence=SignalEvidence(
                metric="roas", actual=2.4, target=2.0, baseline=None, span=WindowSpan.D7
            ),
            gate_verdicts=(),
            emitted_at=NOW,
            rule_code=_BUY_RULE_CODE,
        )
    )
    await session.flush()


async def _seed_broken_measurement(session: AsyncSession, business_id: uuid.UUID) -> None:
    """`unattributed_share` > 0,35 (nodo 1, profitability-engine.md §5):
    2 de 3 atribuciones sin entidad -> 0,667."""
    await session.execute(
        text(
            "INSERT INTO lead_attributions (business_id, hashed_identity, "
            "identity_salt_ref, entity_ref, attribution_rung, conversion_kind, "
            "value_amount, value_currency, occurred_at) VALUES "
            "(:business_id, :h1, 's', NULL, 'aggregate', 'lead', 0, 'EUR', :now), "
            "(:business_id, :h2, 's', NULL, 'aggregate', 'lead', 0, 'EUR', :now), "
            "(:business_id, :h3, 's', NULL, 'aggregate', 'lead', 0, 'EUR', :now)"
        ),
        {
            "business_id": business_id,
            "h1": "a" * 64,
            "h2": "b" * 64,
            "h3": "c" * 64,
            "now": NOW,
        },
    )
    await session.flush()


_PROPOSAL_STATES = text("SELECT state FROM proposals WHERE business_id = :id")
_EXECUTION_OUTCOMES = text("SELECT outcome FROM executions WHERE business_id = :id")


async def test_rule_cycle_proposes_but_never_enqueues_without_owner_approval(
    isolated_database_url: str,
) -> None:
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-b-test/rule-cycle.sock",
    )
    container = Container.build(settings)
    container.clock = FixedClock(NOW)
    try:
        async with rolled_back_session(isolated_database_url) as session:
            entity_ref = campaign_ref(f"c-{NOW.timestamp():.0f}-rc")
            business_id = await seed_entity(session, entity_ref)
            await session.execute(_SET_BUDGET, {"entity_ref": str(entity_ref)})
            await seed_freshness(session, entity_ref, lag_minutes=5)
            await seed_sell_signal(session, entity_ref)
            await calibrate_rule(session, enabled=True)
            await seed_guardrails(
                session,
                scope=GuardrailScope(kind=ScopeKind.ENTITY, ref=str(entity_ref)),
                limits=GuardrailLimits(),
                level="business",
            )

            @asynccontextmanager
            async def _reused_session():
                yield session

            rule_step = LiveRuleStep(
                _reused_session, container.build_execution_use_cases, container.clock
            )
            await rule_step.run(BusinessId(business_id), "test-rule-cycle", NOW)

            proposal_states = [
                row.state
                for row in (await session.execute(_PROPOSAL_STATES, {"id": business_id})).all()
            ]
            assert proposal_states == ["pending"], (
                "Incluso M05=AUTO debe dejar una propuesta para el propietario"
            )

            execution_outcomes = [
                row.outcome
                for row in (await session.execute(_EXECUTION_OUTCOMES, {"id": business_id})).all()
            ]
            assert execution_outcomes == [], "no se encola nada sin aprobacion humana"

            # Pasar el tiempo no convierte la propuesta en autorizacion.
            container.clock.advance_to(NOW + timedelta(seconds=10))  # type: ignore[attr-defined]
            use_cases = container.build_execution_use_cases(session)
            outcome = await use_cases.chokepoint.run_once()

            # No hay trabajo autorizado que entregar al broker.
            assert outcome is None
    finally:
        await container.aclose()


async def test_buy_proposal_is_created_when_measurement_is_healthy(
    isolated_database_url: str,
) -> None:
    """Caso de control: sin `unattributed_share`/`delta_hat` rotos, una
    senal BUY sobre una regla habilitada SI crea una propuesta pendiente."""
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-b-test/measurement-freeze-healthy.sock",
    )
    container = Container.build(settings)
    container.clock = FixedClock(NOW)
    try:
        async with rolled_back_session(isolated_database_url) as session:
            entity_ref = campaign_ref(f"c-{NOW.timestamp():.0f}-buy-healthy")
            business_id = await seed_entity(session, entity_ref)
            await session.execute(_SET_BUDGET, {"entity_ref": str(entity_ref)})
            await _calibrate_buy_rule(session)
            await _seed_buy_signal(session, entity_ref)

            @asynccontextmanager
            async def _reused_session():
                yield session

            rule_step = LiveRuleStep(
                _reused_session, container.build_execution_use_cases, container.clock
            )
            await rule_step.run(BusinessId(business_id), "test-buy-healthy", NOW)

            proposal_states = [
                row.state
                for row in (await session.execute(_PROPOSAL_STATES, {"id": business_id})).all()
            ]
            assert proposal_states == ["pending"]
    finally:
        await container.aclose()


async def test_broken_tracking_freezes_buy(isolated_database_url: str) -> None:
    """T158, profitability-engine.md §5 nodo 1: con `unattributed_share` >
    0,35 la cuenta esta `MEASUREMENT_FROZEN` -- la MISMA senal BUY que en
    `test_buy_proposal_is_created_when_measurement_is_healthy` NO crea
    ninguna propuesta."""
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-b-test/measurement-freeze-broken.sock",
    )
    container = Container.build(settings)
    container.clock = FixedClock(NOW)
    try:
        async with rolled_back_session(isolated_database_url) as session:
            entity_ref = campaign_ref(f"c-{NOW.timestamp():.0f}-buy-frozen")
            business_id = await seed_entity(session, entity_ref)
            await session.execute(_SET_BUDGET, {"entity_ref": str(entity_ref)})
            await _calibrate_buy_rule(session)
            await _seed_buy_signal(session, entity_ref)
            await _seed_broken_measurement(session, business_id)

            @asynccontextmanager
            async def _reused_session():
                yield session

            rule_step = LiveRuleStep(
                _reused_session, container.build_execution_use_cases, container.clock
            )
            await rule_step.run(BusinessId(business_id), "test-buy-frozen", NOW)

            proposal_states = [
                row.state
                for row in (await session.execute(_PROPOSAL_STATES, {"id": business_id})).all()
            ]
            assert proposal_states == [], "BUY debe congelarse: medicion rota (nodo 1)"
    finally:
        await container.aclose()


_REJECT_PROPOSAL = text("UPDATE proposals SET state = 'rejected' WHERE business_id = :id")


_SET_BID_TARGET = text(
    "UPDATE ad_entities SET bid_target_amount_minor = :amount_minor, "
    "bid_target_currency = 'EUR' WHERE entity_ref = :entity_ref"
)


_PROPOSAL_SIGNAL_IDS = text("SELECT signal_id FROM proposals WHERE business_id = :id")


_INSERT_METRICS_DAY = text(
    "INSERT INTO metrics_daily (business_id, entity_ref, entity_level, platform_account_id,"
    " stat_date, account_timezone, currency, spend, conversions_lead)"
    " VALUES (:business_id, :entity_ref, 'campaign', :platform_account_id, :stat_date,"
    " 'Europe/Madrid', 'EUR', :spend, :leads)"
)


_PLATFORM_ACCOUNT_ID = text(
    "SELECT platform_account_id FROM ad_entities WHERE entity_ref = :entity_ref"
)


_SIGNAL_OUTCOME = text(
    "SELECT outcome_source, was_correct FROM signal_outcomes WHERE signal_id = :signal_id"
)


_SIGNAL_OUTCOME_AT_14D = text("SELECT outcome_at_14d FROM signals WHERE id = :signal_id")


async def test_rule_fired_proposal_lets_the_outcome_writer_resolve_past_expired(
    isolated_database_url: str,
) -> None:
    """Regresion T199: antes del fix, `rule_step._resolve_firing` construia
    `Cause(signal_id=None)`, asi que `proposals.signal_id` quedaba NULL para
    TODA propuesta nacida de una regla -- `SqlSignalResolutionPort` nunca
    encontraba la propuesta que de verdad decidio la senal y devolvia
    `EXPIRED` a ciegas, aunque el propietario la hubiera rechazado. Con el
    enlace poblado, `EvaluateSignalOutcomes` (14 dias despues, reloj fijo)
    resuelve `rejected` de verdad -- no el `expired` generico del grupo de
    control gratis."""
    settings = build_api_settings(
        database_url=isolated_database_url,
        broker_socket_path="/tmp/safent-ads-wiring-b-test/rule-cycle-outcome.sock",
    )
    container = Container.build(settings)
    container.clock = FixedClock(NOW)
    try:
        async with rolled_back_session(isolated_database_url) as session:
            entity_ref = campaign_ref(f"c-{NOW.timestamp():.0f}-oc")
            business_id = await seed_entity(session, entity_ref)
            await session.execute(_SET_BUDGET, {"entity_ref": str(entity_ref)})
            await session.execute(
                _SET_BID_TARGET, {"entity_ref": str(entity_ref), "amount_minor": 1000}
            )
            signal_id = await seed_sell_signal(session, entity_ref)
            # APPROVAL (no AUTO): la regla propone y espera al propietario,
            # sin freno/guardarraíl que preparar para llegar a la propuesta.
            await calibrate_rule(session, enabled=True, autonomy_level="APPROVAL")

            @asynccontextmanager
            async def _reused_session():
                yield session

            rule_step = LiveRuleStep(
                _reused_session, container.build_execution_use_cases, container.clock
            )
            await rule_step.run(BusinessId(business_id), "test-rule-cycle-outcome", NOW)

            proposal_signal_ids = [
                row.signal_id
                for row in (await session.execute(_PROPOSAL_SIGNAL_IDS, {"id": business_id})).all()
            ]
            assert proposal_signal_ids == [uuid.UUID(signal_id)]

            # El propietario rechaza la propuesta (decision humana, fuera
            # del alcance de este fix: solo el estado que deja atras importa
            # aqui).
            await session.execute(_REJECT_PROPOSAL, {"id": business_id})

            # CPA muy por encima del objetivo en la ventana: la entidad
            # "salio mala" sin que nadie recortara el gasto (mismos valores
            # que `test_sql_signal_outcome_ports.
            # TestEvaluateSignalOutcomesEndToEnd`).
            account_row = (
                await session.execute(_PLATFORM_ACCOUNT_ID, {"entity_ref": str(entity_ref)})
            ).one()
            await session.execute(
                _INSERT_METRICS_DAY,
                {
                    "business_id": business_id,
                    "entity_ref": str(entity_ref),
                    "platform_account_id": account_row.platform_account_id,
                    "stat_date": NOW.date(),
                    "spend": "100",
                    "leads": 1,
                },
            )
            await session.flush()

            use_case = EvaluateSignalOutcomes(
                due_signals=SqlPendingSignalOutcomesPort(session),
                resolution=SqlSignalResolutionPort(session),
                cpa_window=SqlEntityCpaWindowPort(session),
                action_kinds=SqlRuleActionKindPort(session),
                outcomes=SqlSignalOutcomeRepository(session),
                clock=FixedClock(NOW + timedelta(days=15)),
            )
            evaluated = await use_case.execute(business_id=BusinessId(business_id))
            assert evaluated == 1

            outcome_row = (await session.execute(_SIGNAL_OUTCOME, {"signal_id": signal_id})).one()
            assert outcome_row.outcome_source == "rejected", (
                "sin el enlace signal_id->proposal, esto caia siempre a 'expired' "
                "(rule_step._resolve_firing construia Cause(signal_id=None))"
            )
            assert outcome_row.was_correct is True

            outcome_at_14d = (
                await session.execute(_SIGNAL_OUTCOME_AT_14D, {"signal_id": signal_id})
            ).scalar_one()
            assert outcome_at_14d == {"status": "confirmed"}
    finally:
        await container.aclose()
