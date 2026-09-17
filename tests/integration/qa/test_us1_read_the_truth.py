"""US1 end-to-end (`quickstart.md §5`, tasks.md T054): two businesses, 14
days of ingested facts, the REAL `IngestionCycle`/`SignalCycle` (via
`orchestration.infrastructure.runtime.build_default_runtime`, exactly how
`ads-worker`/the CLI wire them -- only the platform SDK boundary is
doubled, per contracts/platform-port.md "SDK mocked in tests"), plus the
ticker ordering and the cross-business IDOR sweep on the panel read routes.

Two gaps surfaced while writing this bank (both confirmed by reading the
real wiring, not guessed): `data_freshness` is never written by any
production code path, and `LiveSignalStep` never calls
`signals.application.evaluate_entity_signals.EvaluateEntitySignals` (only
`DetectAnomalies` runs). Both are pinned below as `xfail(strict=True)` so a
future wiring fix flips them green and CI forces the removal of the
marker -- see each test's docstring for the exact file/line evidence."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NoReturn

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from safent_ads.accounts.application.ports import (
    AccountRef,
    AdEntitySnapshot,
    AssetUploadRequest,
    EntityStateSnapshot,
    MetricFactSnapshot,
    MetricsRequest,
    PlatformAssetHandle,
    SignedAuthorization,
    WriteIntent,
    WriteOutcome,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money as AccountsMoney
from safent_ads.accounts.domain.refs import IdempotencyKey
from safent_ads.composition.api import _handle_api_error
from safent_ads.composition.container import Container
from safent_ads.execution.infrastructure.sql_freshness import SqlFreshnessPort
from safent_ads.iam.presentation.dependencies import SESSION_COOKIE_NAME
from safent_ads.iam.presentation.errors import ApiError
from safent_ads.notifications.application.publish_ticker import PublishTicker
from safent_ads.notifications.domain.value_objects import ActiveHoursWindow
from safent_ads.notifications.infrastructure.sql_repositories import (
    SqlNotificationOutbox,
    SqlPendingDigest,
    SqlSignalsForTicker,
)
from safent_ads.notifications.testing.fakes import FakeMessenger
from safent_ads.orchestration.infrastructure.runtime import build_default_runtime
from safent_ads.panel.infrastructure.sql_read_model import RequestScopedPanelReadPort
from safent_ads.panel.presentation.deps import AuthenticatedCaller, get_authenticated_caller
from safent_ads.panel.presentation.rest import build_panel_router
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityRef, IdGenerator, UuidIdGenerator
from safent_ads.signals.domain.cause import Cause
from safent_ads.signals.domain.signal import Signal, SignalKind
from safent_ads.signals.domain.value_objects import Evidence as SignalEvidence
from safent_ads.signals.domain.value_objects import MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.infrastructure.sql_repositories import SqlSignalRepository
from tests.contracts.sql_fixtures import account_external_id, campaign_ref, seed_entity
from tests.unit.accounts.application.conftest import FakeOAuthBrokerPort
from tests.unit.composition.factories import build_api_settings

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)
_FACT_DAYS = 14
_DAILY_SPEND_EUR = Decimal("214.37")
_RECONCILIATION_TOLERANCE = Decimal("0.01")
_RAW_TOKEN = "qa-us1-session-token"  # noqa: S105 - fixture, no secreto real


# ---------------------------------------------------------------------------
# Doble del SDK de plataforma (contracts/platform-port.md: "SDK mocked in
# tests"): un inventario + 14 dias de hechos diarios por cuenta, servidos
# desde un diccionario -- nada de red real, ninguna llamada oculta.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AccountFixture:
    entity_ref: EntityRef
    learning_state: LearningState
    daily_spend: Decimal


class _TwoWeeksPlatformPort:
    def __init__(self, accounts: Sequence[_AccountFixture]) -> None:
        self._by_account_key = {
            (fixture.entity_ref.platform, account_external_id(fixture.entity_ref)): fixture
            for fixture in accounts
        }

    def _resolve(self, account_ref: AccountRef) -> _AccountFixture | None:
        """`None` para cualquier cuenta que este doble no conoce -- nunca
        `KeyError`. `IngestionCycle`/`SignalCycle` (plan.md §7) recorren
        TODOS los negocios activos de la base compartida de la sesion de
        pytest (`SqlBusinessListing.list_active_business_ids`), no solo los
        de este banco: con cientos de tests corriendo en la misma base
        (`make test-integration`), levantar `KeyError` aqui por cada
        negocio ajeno dispara `orchestration.application.retry.
        run_with_retry` (3 intentos con espera creciente) UNA VEZ POR
        NEGOCIO AJENO -- con suficientes negocios acumulados, la vuelta
        entera se alarga varios minutos sin que ninguna consulta a
        Postgres quede bloqueada (confirmado con `pg_stat_activity`: cero
        sesiones en espera). Una plataforma real, ante una cuenta que no
        reconoce, no lanza: devuelve "sin datos" -- este doble hace lo
        mismo."""
        return self._by_account_key.get((account_ref.platform, account_ref.external_account_id))

    async def fetch_account_inventory(self, account_ref: AccountRef) -> Sequence[AdEntitySnapshot]:
        fixture = self._resolve(account_ref)
        if fixture is None:
            return []
        return [
            AdEntitySnapshot(
                entity_ref=fixture.entity_ref,
                parent_ref=EntityRef(
                    fixture.entity_ref.platform, fixture.entity_ref.level, "account-parent"
                ),
                name=f"Campana {fixture.entity_ref.external_id}",
                status=AdEntityStatus.ACTIVE,
                is_controllable=True,
                learning_state=fixture.learning_state,
                budget=None,
                bid_target=None,
                shared_budget_ref=None,
                canonical_state={"name": "x", "status": "ACTIVE"},
                fetched_at=_NOW,
            )
        ]

    async def fetch_metrics(self, request: MetricsRequest) -> Sequence[MetricFactSnapshot]:
        fixture = self._resolve(request.account_ref)
        if fixture is None:
            return []
        spend_minor = int(fixture.daily_spend * 100)
        return [
            MetricFactSnapshot(
                entity_ref=fixture.entity_ref,
                stat_date=(_NOW - timedelta(days=offset)).date(),
                stat_hour=None,
                currency="EUR",
                spend=AccountsMoney(spend_minor, "EUR"),
                impressions=1_000 + offset,
                clicks=40,
                reach=None,
                frequency=None,
                conversions_by_kind={"lead": 2},
                conversion_value=None,
                video_views_3s=None,
                video_views_75pct=None,
                search_lost_is_budget=None,
                search_lost_is_rank=None,
            )
            for offset in range(_FACT_DAYS)
        ]

    async def read_entity_state(self, entity_ref: EntityRef) -> EntityStateSnapshot:
        raise NotImplementedError

    async def upload_asset(self, request: AssetUploadRequest) -> PlatformAssetHandle:
        raise NotImplementedError

    async def execute_write(
        self,
        intent: WriteIntent,
        authorization: SignedAuthorization,
        idempotency_key: IdempotencyKey,
    ) -> WriteOutcome:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Fixture compartida: dos negocios reales, cada uno con una cuenta/campana,
# 14 dias de hechos ingeridos por el `IngestionCycle` REAL.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TwoBusinesses:
    session_factory: async_sessionmaker[AsyncSession]
    business_normal: uuid.UUID
    entity_normal: EntityRef
    business_learning: uuid.UUID
    entity_learning: EntityRef
    id_generator: IdGenerator


def _unused_execution_use_cases_factory(session: AsyncSession) -> NoReturn:
    """`build_default_runtime` exige una fabrica de casos de uso de
    ejecucion para construir `RuleCycle`/`ExecutionCycle` -- ninguno de los
    dos se invoca en este banco (solo `ingestion_cycle`/`signal_cycle`), asi
    que no hace falta un `Container`/motor completo (con su propio pool)
    solo para satisfacer la firma. Ver `composition/database.py`: "crear un
    motor por caso de uso agota conexiones y rompe pool_pre_ping" -- un
    `Container.build()` por test (5 en este fichero) sobre un Postgres YA
    compartido por cientos de tests previos en `make test-integration` es
    exactamente ese patron; con un unico motor (`session_factory` de abajo,
    el mismo que siembra/ingiere/limpia) el banco deja de contribuir a esa
    acumulacion."""
    del session
    raise NotImplementedError("no se invoca: este banco no ejercita RuleCycle/ExecutionCycle")


@pytest.fixture
async def two_businesses_with_two_weeks_of_facts(
    isolated_database_url: str,
) -> AsyncIterator[_TwoBusinesses]:
    """`isolated_database_url`, NO `database_url`: `IngestionCycle`/
    `SignalCycle` recorren TODOS los negocios activos via
    `SqlBusinessListing.list_active_business_ids()`, una consulta GLOBAL
    sin filtro de negocio -- exactamente el motivo por el que
    `tests/conftest.py::isolated_database_url` existe ("los casos cuya
    consulta es GLOBAL por diseno"), igual que el reclamo de la cola de
    ejecuciones. Sobre la base COMPARTIDA (`database_url`, cientos de
    negocios acumulados por el resto de `make test-integration`), cada
    negocio ajeno anade una vuelta mas -- lento, no un cuelgue, pero sin
    limite util en un banco que no lo necesita."""
    entity_normal = campaign_ref(f"n-{uuid.uuid4().hex[:10]}", platform_value="google")
    entity_learning = campaign_ref(f"l-{uuid.uuid4().hex[:10]}", platform_value="meta")
    engine = create_async_engine(isolated_database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    clock = FixedClock(_NOW)

    async with session_factory() as session:
        business_normal = await seed_entity(session, entity_normal)
        business_learning = await seed_entity(session, entity_learning)
        await session.commit()

    platform_port = _TwoWeeksPlatformPort(
        [
            _AccountFixture(entity_normal, LearningState.NOT_APPLICABLE, _DAILY_SPEND_EUR),
            _AccountFixture(entity_learning, LearningState.LEARNING, _DAILY_SPEND_EUR),
        ]
    )
    try:
        runtime = build_default_runtime(
            session_factory=session_factory,
            platform_port=platform_port,
            oauth_broker=FakeOAuthBrokerPort(),
            active_hours=ActiveHoursWindow.parse("00:00-23:59", tz_name="UTC"),
            execution_use_cases_factory=_unused_execution_use_cases_factory,
            clock=clock,
            id_generator=UuidIdGenerator(),
        )
        ingestion_report = await runtime.ingestion_cycle.execute()
        assert ingestion_report.all_succeeded, ingestion_report.failed_results
        signal_report = await runtime.signal_cycle.execute()
        assert signal_report.all_succeeded, signal_report.failed_results

        yield _TwoBusinesses(
            session_factory=session_factory,
            business_normal=business_normal,
            entity_normal=entity_normal,
            business_learning=business_learning,
            entity_learning=entity_learning,
            id_generator=UuidIdGenerator(),
        )
    finally:
        # `engine.dispose()` va AL FINAL, despues de limpiar: sembrar, el
        # ciclo y la limpieza comparten el mismo motor/pool de principio a
        # fin -- un unico `create_async_engine` por test, no dos.
        async with session_factory() as session:
            for business_id in (business_normal, business_learning):
                await _delete_business(session, business_id)
            await session.commit()
        await engine.dispose()


async def _delete_business(session: AsyncSession, business_id: uuid.UUID) -> None:
    params = {"id": business_id}
    await session.execute(text("DELETE FROM metrics_daily WHERE business_id = :id"), params)
    await session.execute(text("DELETE FROM anomalies WHERE business_id = :id"), params)
    await session.execute(text("DELETE FROM signals WHERE business_id = :id"), params)
    await session.execute(text("DELETE FROM notifications WHERE business_id = :id"), params)
    await session.execute(text("DELETE FROM ad_entities WHERE business_id = :id"), params)
    credential_ref_ids = (
        await session.execute(
            text("SELECT credential_ref_id FROM platform_accounts WHERE business_id = :id"),
            params,
        )
    ).scalars().all()
    await session.execute(text("DELETE FROM platform_accounts WHERE business_id = :id"), params)
    if credential_ref_ids:
        await session.execute(
            text("DELETE FROM credential_refs WHERE id = ANY(:ids)"),
            {"ids": list(credential_ref_ids)},
        )
    await session.execute(text("DELETE FROM businesses WHERE id = :id"), params)


async def _seed_second_entity_in_business(
    session: AsyncSession, *, business_id: uuid.UUID, entity_ref: EntityRef
) -> None:
    """Misma forma que `tests.contracts.sql_fixtures.seed_entity`, pero
    cuelga la cuenta/entidad nueva de un negocio YA sembrado en vez de
    crear uno -- para que dos campanas compitan en el mismo ticker."""
    credential_id = uuid.uuid4()
    account_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:12]
    await session.execute(
        text("INSERT INTO credential_refs (id, platform, alias) VALUES (:id, :platform, :alias)"),
        {"id": credential_id, "platform": entity_ref.platform.value, "alias": f"alias-{suffix}"},
    )
    await session.execute(
        text(
            """
            INSERT INTO platform_accounts (id, business_id, platform, external_account_id,
                                           currency, timezone, api_tier, credential_ref_id,
                                           status)
            VALUES (:id, :business_id, :platform, :external_account_id, 'EUR',
                    'Europe/Madrid', 'meta_full', :credential_ref_id, 'ACTIVE')
            """
        ),
        {
            "id": account_id,
            "business_id": business_id,
            "platform": entity_ref.platform.value,
            "external_account_id": account_external_id(entity_ref),
            "credential_ref_id": credential_id,
        },
    )
    await session.execute(
        text(
            """
            INSERT INTO ad_entities (business_id, platform_account_id, platform, level,
                                     external_id, name, status, platform_state_hash)
            VALUES (:business_id, :account_id, :platform, :level, :external_id,
                    :name, 'ACTIVE', :state_hash)
            """
        ),
        {
            "business_id": business_id,
            "account_id": account_id,
            "platform": entity_ref.platform.value,
            "level": entity_ref.level.value,
            "external_id": entity_ref.external_id,
            # Mismo patron que `_TwoWeeksPlatformPort.fetch_account_inventory`
            # (arriba, en este mismo fichero): el ticker renderiza
            # `entity_name` desde `ad_entities.name`, y el test compara contra
            # `f"Campana {entity_ref.external_id}"` -- un nombre fijo sin
            # relacion con `external_id` nunca podia encontrarse en el cuerpo.
            "name": f"Campana {entity_ref.external_id}",
            "state_hash": "a" * 64,
        },
    )
    await session.flush()


# ---------------------------------------------------------------------------
# quickstart §5.1/§5.3: `IngestionCycle` real ingiere 14 dias, y el gasto
# ingerido cuadra con la verdad de plataforma (aqui, el doble) por debajo
# del 1 % -- el proxy automatizable del cuadre manual contra Google
# Ads/Meta (ver el informe final: ese cruce SI requiere cuentas reales y
# queda fuera del alcance de un test automatizado).
# ---------------------------------------------------------------------------


async def test_ingestion_cycle_reconciles_ingested_spend_within_one_percent(
    two_businesses_with_two_weeks_of_facts: _TwoBusinesses,
) -> None:
    fixture = two_businesses_with_two_weeks_of_facts
    platform_truth = _DAILY_SPEND_EUR * _FACT_DAYS

    async with fixture.session_factory() as session:
        ingested = (
            await session.execute(
                text(
                    "SELECT COALESCE(SUM(spend), 0) AS total FROM metrics_daily "
                    "WHERE entity_ref = :entity_ref"
                ),
                {"entity_ref": str(fixture.entity_normal)},
            )
        ).scalar_one()

    ingested_eur = Decimal(ingested) / Decimal(100)
    deviation = abs(ingested_eur - platform_truth) / platform_truth
    assert deviation < _RECONCILIATION_TOLERANCE, (
        f"cuadre fuera de tolerancia: plataforma={platform_truth} ingerido={ingested_eur}"
    )


# ---------------------------------------------------------------------------
# quickstart §5.6: el ticker ordena por dinero en juego descendente. Se
# ejercita `PublishTicker` (el mismo caso de uso que `NotificationCycle`
# invoca por negocio, orchestration/application/notification_cycle.py)
# directamente sobre adaptadores SQL reales -- solo el mensajero de
# Telegram esta sustituido (`FakeMessenger`, la misma doble que usan los
# tests unitarios de rendering, `notifications/testing/fakes.py`), igual
# criterio de "SDK mocked" que el resto de este banco.
#
# `SqlSignalsForTicker` filtra por reloj de PARED real (`_now_utc()` en
# `notifications/infrastructure/sql_repositories.py`), no por el `Clock`
# inyectado -- las senales se siembran con `datetime.now(UTC)`, no con
# `_NOW`, a proposito.
#
# GAP 3 (fijado): `SqlNotificationOutbox._UPSERT_NOTIFICATION` ahora incluye
# la columna `message_id` y `SqlNotificationOutbox.save` pasa
# `notification.platform_message_id` -- una entrega SENT ya no viola
# `notifications_sent_is_traceable_check` (0010_notifications.py) al
# persistirse contra Postgres real.
# ---------------------------------------------------------------------------


async def test_ticker_orders_actionable_signals_by_money_at_stake_descending(
    two_businesses_with_two_weeks_of_facts: _TwoBusinesses,
) -> None:
    fixture = two_businesses_with_two_weeks_of_facts
    entity_cheap = fixture.entity_normal
    entity_expensive = campaign_ref(f"m-{uuid.uuid4().hex[:10]}", platform_value="google")
    real_now = datetime.now(UTC)

    async with fixture.session_factory() as session:
        # Segunda campana sembrada DIRECTAMENTE en el negocio ya existente
        # (no via `seed_entity`, que siempre crea un negocio nuevo): el
        # UPSERT de `SqlSignalRepository.save` deriva `signals.business_id`
        # de `ad_entities.business_id` en el momento de guardar (ver
        # `_UPSERT_SIGNAL`), asi que la entidad debe pertenecer al negocio
        # correcto ANTES de guardar la senal, no despues.
        await _seed_second_entity_in_business(
            session, business_id=fixture.business_normal, entity_ref=entity_expensive
        )
        await session.execute(
            text("DELETE FROM signals WHERE entity_ref IN (:cheap, :expensive)"),
            {"cheap": str(entity_cheap), "expensive": str(entity_expensive)},
        )
        cheap_signal = Signal(
            entity_ref=entity_cheap,
            kind=SignalKind.SELL,
            strength=SignalStrength(60),
            cause=Cause.ROAS_BELOW_TARGET_SUSTAINED,
            cause_sentence="ROAS por debajo del objetivo en 7D",
            span=WindowSpan.D7,
            money_at_stake=MoneyAtStake(minor_units=5_000, currency="EUR"),
            evidence=SignalEvidence(
                metric="roas", actual=1.6, target=2.0, baseline=None, span=WindowSpan.D7
            ),
            gate_verdicts=(),
            emitted_at=real_now,
            rule_code="M05",
        )
        expensive_signal = Signal(
            entity_ref=entity_expensive,
            kind=SignalKind.SELL,
            strength=SignalStrength(90),
            cause=Cause.ROAS_BELOW_TARGET_SUSTAINED,
            cause_sentence="ROAS muy por debajo del objetivo en 7D",
            span=WindowSpan.D7,
            money_at_stake=MoneyAtStake(minor_units=250_000, currency="EUR"),
            evidence=SignalEvidence(
                metric="roas", actual=0.8, target=2.0, baseline=None, span=WindowSpan.D7
            ),
            gate_verdicts=(),
            emitted_at=real_now,
            rule_code="M05",
        )
        signal_repo = SqlSignalRepository(session, cycle_id=uuid.uuid4())
        await signal_repo.save(cheap_signal)
        await signal_repo.save(expensive_signal)
        await session.commit()

        messenger = FakeMessenger()
        active_hours = ActiveHoursWindow.parse("00:00-23:59", tz_name="UTC")
        publish_ticker = PublishTicker(
            signals=SqlSignalsForTicker(session),
            outbox=SqlNotificationOutbox(session),
            pending_digest=SqlPendingDigest(
                session, active_hours=active_hours, digest_hour_default=8
            ),
            messenger=messenger,
            id_generator=UuidIdGenerator(),
            active_hours=active_hours,
        )
        await publish_ticker.execute(
            business_id=BusinessId(fixture.business_normal),
            business_name="Negocio de contrato",
            owner_chat_ids=[111222333],
            now=real_now,
        )
        await session.commit()

    assert len(messenger.sent) == 1
    body = messenger.sent[0].text
    cheap_index = body.index(f"Campana {entity_cheap.external_id}")
    expensive_index = body.index(f"Campana {entity_expensive.external_id}")
    assert expensive_index < cheap_index, "el ticker debe listar primero el mayor dinero en juego"


# ---------------------------------------------------------------------------
# quickstart §5 (IDOR transversal, C-27/C-31): la sesion de un propietario
# restringida a un negocio nunca ve datos de lectura del otro a traves de
# `/freshness`/`/portfolio` -- mismo patron de
# `tests/integration/composition/test_idor_sweep_new_routes.py`, aplicado
# a los routers de `panel` en vez de `execution`.
# ---------------------------------------------------------------------------


def _panel_client(
    container: Container, *, allowed_business_id: uuid.UUID
) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.container = container
    app.add_exception_handler(ApiError, _handle_api_error)
    app.include_router(build_panel_router(RequestScopedPanelReadPort(container.session_factory)))

    async def _restricted() -> AuthenticatedCaller:
        return AuthenticatedCaller(allowed_business_ids=frozenset({str(allowed_business_id)}))

    app.dependency_overrides[get_authenticated_caller] = _restricted
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://test", cookies={SESSION_COOKIE_NAME: _RAW_TOKEN}
    )


async def test_idor_denies_freshness_and_portfolio_across_businesses(
    two_businesses_with_two_weeks_of_facts: _TwoBusinesses, isolated_database_url: str
) -> None:
    fixture = two_businesses_with_two_weeks_of_facts
    # Mismo `isolated_database_url` que el fixture: los negocios de este
    # banco viven ahi, no en la base compartida de `database_url`.
    settings = build_api_settings(database_url=isolated_database_url)
    container = Container.build(settings)
    try:
        async with _panel_client(
            container, allowed_business_id=fixture.business_normal
        ) as client:
            own_freshness = await client.get(
                "/api/v1/freshness", params={"business_id": str(fixture.business_normal)}
            )
            assert own_freshness.status_code == 200, own_freshness.text

            foreign_freshness = await client.get(
                "/api/v1/freshness", params={"business_id": str(fixture.business_learning)}
            )
            assert foreign_freshness.status_code == 404, (
                "una sesion sin acceso al negocio debe recibir 404, nunca los datos"
            )

            foreign_portfolio = await client.get(
                "/api/v1/portfolio", params={"business_id": str(fixture.business_learning)}
            )
            assert foreign_portfolio.status_code == 404

            foreign_signals = await client.get(
                "/api/v1/signals", params={"business_id": str(fixture.business_learning)}
            )
            assert foreign_signals.status_code == 404
    finally:
        await container.aclose()


# ---------------------------------------------------------------------------
# GAP 1 (fijado): `LiveIngestionStep` ahora invoca `ComputeFreshness` por
# cuenta/nivel tras ingerir, y `metrics.infrastructure.sql_repositories.
# SqlFreshnessRepository` (nuevo) lo persiste en `data_freshness` --
# contraparte de escritura de `execution/infrastructure/sql_freshness.py::
# SqlFreshnessPort`, que solo lee. `GET /api/v1/freshness`
# (quickstart.md §5.2) ve la fila que acaba de crear la ingesta real.
# ---------------------------------------------------------------------------


async def test_freshness_reflects_real_ingestion_cycle(
    two_businesses_with_two_weeks_of_facts: _TwoBusinesses,
) -> None:
    fixture = two_businesses_with_two_weeks_of_facts
    async with fixture.session_factory() as session:
        freshness_port = SqlFreshnessPort(session, FixedClock(_NOW))
        last_ingested_at = await freshness_port.last_ingested_at(fixture.entity_normal)

    assert last_ingested_at is not None, (
        "el IngestionCycle real debe dejar rastro en data_freshness"
    )
    lag_minutes = int((_NOW - last_ingested_at).total_seconds() // 60)
    assert lag_minutes < 60


# ---------------------------------------------------------------------------
# GAP 2 (fijado): `LiveSignalStep` ahora tambien invoca `EvaluateEntitySignals`
# por entidad de nivel campana, con `GateContext`/`SignalTargets` construidos
# desde `AdEntity`/`PlatformAccount` reales (orchestration/infrastructure/
# live_steps.py). Una entidad en `learning_state = LEARNING`
# (quickstart.md §5.5) recibe, a traves del ciclo real, un `HOLD` con
# `gate_reason` de `LearningGate`.
# ---------------------------------------------------------------------------


async def test_learning_gated_entity_gets_a_hold_signal_via_real_cycle(
    two_businesses_with_two_weeks_of_facts: _TwoBusinesses,
) -> None:
    fixture = two_businesses_with_two_weeks_of_facts
    async with fixture.session_factory() as session:
        signal_repo = SqlSignalRepository(session, cycle_id=uuid.uuid4())
        latest = await signal_repo.find_latest_for_entity(entity_ref=fixture.entity_learning)

    assert latest is not None, (
        "el SignalCycle real nunca emitio ninguna senal para la entidad en aprendizaje"
    )
    assert latest.kind is SignalKind.HOLD
    blocked = [verdict for verdict in latest.gate_verdicts if not verdict.passed]
    assert blocked and "aprendizaje" in blocked[0].reason
