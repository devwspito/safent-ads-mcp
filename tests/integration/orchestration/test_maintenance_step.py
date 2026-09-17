"""`LiveMaintenanceStep` (tasks.md T078) contra Postgres real. Los tres
pasos globales (`expire_stale_proposals`/`purge_telegram_artifacts`/
`verify_decision_log_chain`) usan `isolated_database_url`: son consultas
GLOBALES por diseno (sin filtro de negocio), igual criterio que
`tests/integration/qa/test_us1_read_the_truth.py`."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from safent_ads.metrics.domain.conversion_kind import ConversionKind as MetricsConversionKind
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.infrastructure.sql_repositories import SqlMetricFactRepository
from safent_ads.orchestration.infrastructure.maintenance_step import LiveMaintenanceStep
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.signals.domain.cause import Cause
from safent_ads.signals.domain.gate_verdict import GateName, GateVerdict
from safent_ads.signals.domain.signal import Signal, SignalKind
from safent_ads.signals.domain.value_objects import Evidence, MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.infrastructure.sql_repositories import SqlSignalRepository
from tests.conftest import OwnerFactory
from tests.contracts.sql_fixtures import campaign_ref, seed_entity

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(
    isolated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


_INSERT_MINIMAL_PROPOSAL = text("""
    INSERT INTO proposals (
        id, business_id, entity_ref, parameter, current_value, proposed_value, diff_hash,
        classification, cause_key, cause, estimated_impact_amount, estimated_impact_currency,
        urgency, state, expires_at
    )
    VALUES (
        :id, :business_id, :entity_ref, :parameter, CAST(:current_value AS JSONB),
        CAST(:proposed_value AS JSONB), :diff_hash, 'routine', :cause_key, 'causa de prueba',
        0, 'EUR', 'minor', 'pending', :expires_at
    )
""")


async def _insert_pending_proposal(
    session: AsyncSession, *, business_id: uuid.UUID, entity_ref: EntityRef, expires_at: datetime
) -> uuid.UUID:
    proposal_id = uuid.uuid4()
    await session.execute(
        _INSERT_MINIMAL_PROPOSAL,
        {
            "id": proposal_id,
            "business_id": business_id,
            "entity_ref": str(entity_ref),
            "parameter": f"param-{proposal_id.hex[:8]}",
            "current_value": '{"type":"json","value":null}',
            "proposed_value": '{"type":"json","value":{}}',
            "diff_hash": "a" * 64,
            "cause_key": f"test:M05:{entity_ref}",
            "expires_at": expires_at,
        },
    )
    return proposal_id


async def test_expire_stale_proposals_only_expires_what_is_due(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    entity_ref = campaign_ref(f"maint-{uuid.uuid4().hex[:8]}")
    past = _NOW - timedelta(hours=1)
    future = _NOW + timedelta(hours=1)
    async with session_factory() as session:
        business_id = await seed_entity(session, entity_ref)
        due_id = await _insert_pending_proposal(
            session, business_id=business_id, entity_ref=entity_ref, expires_at=past
        )
        not_due_id = await _insert_pending_proposal(
            session, business_id=business_id, entity_ref=entity_ref, expires_at=future
        )
        await session.commit()

    step = LiveMaintenanceStep(session_factory, FixedClock(_NOW))
    await step.expire_stale_proposals(_NOW)

    async with session_factory() as session:
        due_state = (
            await session.execute(
                text("SELECT state FROM proposals WHERE id = :id"), {"id": due_id}
            )
        ).scalar_one()
        not_due_state = (
            await session.execute(
                text("SELECT state FROM proposals WHERE id = :id"), {"id": not_due_id}
            )
        ).scalar_one()
    assert due_state == "expired"
    assert not_due_state == "pending"


async def test_purge_telegram_artifacts_removes_only_what_expired(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    entity_ref = campaign_ref(f"maint-tg-{uuid.uuid4().hex[:8]}")
    async with session_factory() as session:
        business_id = await seed_entity(session, entity_ref)
        proposal_id = await _insert_pending_proposal(
            session,
            business_id=business_id,
            entity_ref=entity_ref,
            expires_at=_NOW + timedelta(days=1),
        )
        owner_id = await OwnerFactory(session).create()

        # `created_at` explicito: los CHECK de TTL (`expires_at > created_at`)
        # comparan contra el `now()` real del servidor, no contra `_NOW`.
        created_at = _NOW - timedelta(days=2)
        expired_nonce = uuid.uuid4().hex[:10]
        alive_nonce = uuid.uuid4().hex[:10]
        await session.execute(
            text(
                "INSERT INTO telegram_callbacks (nonce, proposal_id, chat_id, message_id, "
                "diff_hash, action, expires_at, created_at) VALUES (:nonce, :proposal_id, 1, 1, "
                ":diff_hash, 'a', :expires_at, :created_at)"
            ),
            {
                "nonce": expired_nonce,
                "proposal_id": proposal_id,
                "diff_hash": "b" * 64,
                "expires_at": _NOW - timedelta(hours=1),
                "created_at": created_at,
            },
        )
        await session.execute(
            text(
                "INSERT INTO telegram_callbacks (nonce, proposal_id, chat_id, message_id, "
                "diff_hash, action, expires_at, created_at) VALUES (:nonce, :proposal_id, 1, 1, "
                ":diff_hash, 'a', :expires_at, :created_at)"
            ),
            {
                "nonce": alive_nonce,
                "proposal_id": proposal_id,
                "diff_hash": "c" * 64,
                "expires_at": _NOW + timedelta(hours=1),
                "created_at": created_at,
            },
        )
        expired_brake_nonce = uuid.uuid4().hex[:10]
        await session.execute(
            text(
                "INSERT INTO telegram_brake_confirmations (nonce, chat_id, pending_action, "
                "expires_at, created_at) VALUES (:nonce, 1, 'on', :expires_at, :created_at)"
            ),
            {
                "nonce": expired_brake_nonce,
                "expires_at": _NOW - timedelta(minutes=1),
                "created_at": created_at,
            },
        )
        await session.execute(
            text(
                "INSERT INTO telegram_owner_chats (owner_id, status, pairing_code_hash, "
                "code_expires_at) VALUES (:owner_id, 'pending', 'hash', :code_expires_at)"
            ),
            {"owner_id": owner_id, "code_expires_at": _NOW - timedelta(minutes=1)},
        )
        await session.commit()

    step = LiveMaintenanceStep(session_factory, FixedClock(_NOW))
    await step.purge_telegram_artifacts(_NOW)

    async with session_factory() as session:
        remaining_nonces = {
            row[0]
            for row in (
                await session.execute(text("SELECT nonce FROM telegram_callbacks"))
            ).all()
        }
        remaining_brake_nonces = {
            row[0]
            for row in (
                await session.execute(text("SELECT nonce FROM telegram_brake_confirmations"))
            ).all()
        }
        pairing_row = (
            await session.execute(
                text(
                    "SELECT status, pairing_code_hash, code_expires_at FROM telegram_owner_chats "
                    "WHERE owner_id = :owner_id"
                ),
                {"owner_id": owner_id},
            )
        ).one()

    assert expired_nonce not in remaining_nonces
    assert alive_nonce in remaining_nonces
    assert expired_brake_nonce not in remaining_brake_nonces
    assert pairing_row.status == "unpaired"
    assert pairing_row.pairing_code_hash is None
    assert pairing_row.code_expires_at is None


async def test_verify_decision_log_chain_does_not_raise_on_an_empty_chain(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    step = LiveMaintenanceStep(session_factory, FixedClock(_NOW))

    await step.verify_decision_log_chain(_NOW)


def _fact(
    entity_ref: EntityRef, *, stat_date: date, conversions: int, spend_minor: int
) -> MetricFact:
    return MetricFact(
        entity_ref=entity_ref,
        stat_date=stat_date,
        account_timezone="Europe/Madrid",
        currency="EUR",
        spend_minor=spend_minor,
        impressions=5_000,
        clicks=300,
        reach=2_500,
        conversions=MappingProxyType({MetricsConversionKind.BUSINESS_CONVERSION: conversions}),
        ingested_at=_NOW,
    )


async def test_reconcile_platform_vs_crm_marks_a_contradicted_signal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    entity_ref = campaign_ref(f"maint-recon-{uuid.uuid4().hex[:8]}")
    emitted_at = _NOW - timedelta(days=5)  # mas alla del rezago de 3 dias
    window_end = emitted_at.date()
    async with session_factory() as session:
        business_id = await seed_entity(session, entity_ref)
        # Un unico hecho, todas las conversiones de golpe: `ReconcilePlatformVsCrm`
        # solo suma lo que haya en la ventana. Repartirlo en 7 filas
        # consecutivas (una por dia) le da a `SignalCycle` (que corre sobre
        # el mismo Postgres en `test_us1_read_the_truth.py`, comparte
        # `isolated_database_url`) exactamente un punto por dia de la
        # semana -- el detector `weekday_z` divide por una desviacion
        # tipica de un unico punto (0) y el z-score sale `-inf`,
        # `NUMERIC(10,4)` no lo admite. Una sola fila deja la serie del
        # mismo dia de la semana vacia (`ZeroDivisionError`, ya manejado).
        await SqlMetricFactRepository(session).upsert_many(
            [_fact(entity_ref, stat_date=window_end, conversions=14, spend_minor=50_000)]
        )
        # 14 conversiones reportadas por la plataforma, cero en el CRM: el
        # umbral (T116) exige volumen minimo (5) y razon crm/plataforma <0.5.
        signal = Signal(
            entity_ref=entity_ref,
            kind=SignalKind.BUY,
            strength=SignalStrength(70),
            cause=Cause.ROAS_BELOW_TARGET_SUSTAINED,
            cause_sentence="Evidencia suficiente para escalar.",
            span=WindowSpan.D7,
            money_at_stake=MoneyAtStake(minor_units=31000, currency="EUR"),
            evidence=Evidence(
                metric="roas", actual=2.4, target=2.0, baseline=None, span=WindowSpan.D7
            ),
            gate_verdicts=(GateVerdict.ok(GateName.LEARNING),),
            emitted_at=emitted_at,
            rule_code="M05",
        )
        await SqlSignalRepository(session, cycle_id=uuid.uuid4()).save(signal)
        await session.commit()

    step = LiveMaintenanceStep(session_factory, FixedClock(_NOW))
    await step.reconcile_platform_vs_crm(BusinessId(business_id), "cycle-1", _NOW)

    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT contradicted_at FROM signals WHERE business_id = :business_id "
                    "AND entity_ref = :entity_ref"
                ),
                {"business_id": business_id, "entity_ref": str(entity_ref)},
            )
        ).one()
        outcome = (
            await session.execute(
                text("SELECT was_correct FROM signal_outcomes WHERE business_id = :business_id"),
                {"business_id": business_id},
            )
        ).one()
    assert row.contradicted_at is not None
    assert outcome.was_correct is False
