"""Adaptadores reales de `orchestration/application/ports.py` (integracion),
cableados por `orchestration.infrastructure.runtime.build_default_runtime`.
Una sesion (unidad de trabajo) por llamada a
`run()` -- por negocio y ciclo, `cycle_step_runner.run_step_for_business`
ya aisla el fallo de un negocio del resto (plan.md §7: "el fallo de un
negocio no debe abortar el ciclo para los demas")."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from types import MappingProxyType

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.application.ports import (
    AdsPlatformPort,
    DateWindow,
    MetricFactSnapshot,
    MetricGranularity,
    MetricsRequest,
)
from safent_ads.accounts.application.sync_account_inventory import SyncAccountInventory
from safent_ads.accounts.domain.ad_entity import AdEntity
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.platform_account import PlatformAccount
from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.accounts.infrastructure.sql_repositories import (
    SqlAccountRepository,
    SqlAdEntityRepository,
)
from safent_ads.economics.domain.lag_curve import resolve_median_lag_days
from safent_ads.economics.infrastructure.sql_repositories import SqlLagCurveRepository
from safent_ads.metrics.application.compute_freshness import (
    ComputeFreshness,
    ComputeFreshnessRequest,
)
from safent_ads.metrics.application.errors import NoMetricsIngestedError
from safent_ads.metrics.application.ingest_daily_metrics import (
    IngestDailyMetrics,
    IngestDailyMetricsRequest,
)
from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.infrastructure.sql_repositories import (
    SqlFreshnessRepository,
    SqlMetricFactRepository,
)
from safent_ads.orchestration.application.ports import NotificationTarget
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from safent_ads.signals.application.detect_anomalies import DetectAnomalies, DetectAnomaliesRequest
from safent_ads.signals.application.evaluate_entity_signals import (
    EvaluateEntitySignals,
    EvaluateEntitySignalsRequest,
    GateContext,
    SignalTargets,
)
from safent_ads.signals.domain.gates import LearningStatus
from safent_ads.signals.infrastructure.sql_repositories import (
    SqlAnomalyRepository,
    SqlDailySpendSeriesRepository,
    SqlMetricWindowRepository,
    SqlSignalRepository,
)

logger = structlog.get_logger(__name__)

_INGESTION_WINDOW_DAYS = 2  # hoy + ayer: suficiente para frescura NFR-1

# Umbrales de puertas (rule-catalog-and-signals.md §1 "Min data"/"Cooldown"):
# kill decision needs >=5-10 conversions OR spend >=5-10x target CPA; M13 fija
# 24h de cooldown. Los mismos valores que `tests/unit/signals/
# test_evaluate_entity_signals.py::_gate_context` -- "hoy `target_cpa_minor`
# es constante" (profitability-engine.md): calibrarlo por cuenta sigue
# siendo T157/futuro (no hay `target_cpa` por entidad todavia), fuera de
# esta lane de wiring.
_DEFAULT_COOLDOWN = timedelta(hours=24)
# T157: solo el FALLBACK cuando `resolve_median_lag_days` no encuentra una
# curva calibrada (`_resolve_median_lag_by_platform`, mas abajo) -- ya no
# el unico valor que ve `AttributionLagGate`.
_DEFAULT_MEDIAN_LAG_DAYS = 3
_DEFAULT_MIN_SPEND_MULTIPLE = 5.0
_DEFAULT_MIN_IMPRESSIONS = 1_000
_DEFAULT_MIN_CONVERSIONS = 10

_LEARNING_STATUS_BY_STATE: dict[LearningState, LearningStatus] = {
    LearningState.LEARNING: LearningStatus.LEARNING,
    LearningState.LEARNED: LearningStatus.SUCCESS,
    LearningState.NOT_APPLICABLE: LearningStatus.SUCCESS,
}

_SELECT_ACTIVE_BUSINESSES = text(
    "SELECT id FROM businesses WHERE is_active = true ORDER BY created_at"
)
_SELECT_ACTIVE_BUSINESSES_WITH_NAME = text(
    "SELECT id, name FROM businesses WHERE is_active = true ORDER BY created_at"
)


def _cycle_uuid(cycle_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(cycle_id)
    except ValueError:
        # `cycle_id` puede ser cualquier cadena (T047 no le exige forma de
        # UUID); las tablas de senales/anomalias si lo exigen, asi que se
        # deriva uno estable a partir de la cadena en vez de fallar el ciclo.
        return uuid.uuid5(uuid.NAMESPACE_URL, cycle_id)


class SqlBusinessListing:
    """`BusinessListingPort` sobre `businesses.is_active`."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_active_business_ids(self) -> list[BusinessId]:
        async with self._session_factory() as session:
            rows = (await session.execute(_SELECT_ACTIVE_BUSINESSES)).all()
        return [BusinessId(row.id) for row in rows]


class SqlNotificationTargets:
    """`NotificationTargetsPort`: negocios activos + chats del propietario.

    Modelo de propietario unico (data-model.md): no hay
    `telegram_owner_chats` por negocio todavia (contracts/rest-api.md la
    lista entre las migraciones futuras de `0013_panel_contract`) -- los
    mismos `owner_chat_ids` de `WorkerSettings` sirven a todo negocio."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], owner_chat_ids: tuple[int, ...]
    ) -> None:
        self._session_factory = session_factory
        self._owner_chat_ids = owner_chat_ids

    async def list_notification_targets(self) -> list[NotificationTarget]:
        async with self._session_factory() as session:
            rows = (await session.execute(_SELECT_ACTIVE_BUSINESSES_WITH_NAME)).all()
        return [
            NotificationTarget(
                business_id=BusinessId(row.id),
                business_name=row.name,
                owner_chat_ids=self._owner_chat_ids,
            )
            for row in rows
        ]


class LiveIngestionStep:
    """`IngestionStepPort`: sincroniza inventario, trae metricas diarias de
    hoy/ayer por cada cuenta del negocio via el broker
    (`AdsPlatformPort.fetch_account_inventory`/`fetch_metrics`) y deja
    rastro en `data_freshness` (NFR-1: `GET /api/v1/freshness`, el banner
    del panel y `get_project_context` del MCP leen de ahi).

    Simplificacion documentada: sin distincion horaria/reproceso de 28
    dias todavia (plan.md §7 completo es `IngestionCycle` T047+reproceso,
    fuera del alcance de esta lane) -- cubre lo que
    `quickstart.md §5` pide comprobar (frescura, cuadre de gasto de ayer)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        platform_port: AdsPlatformPort,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._platform_port = platform_port
        self._clock = clock

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None:
        del cycle_id, now
        today = self._clock.now().date()
        window = DateWindow(start=today - timedelta(days=_INGESTION_WINDOW_DAYS - 1), end=today)
        async with self._session_factory() as session:
            accounts = await SqlAccountRepository(session).list_for_observation(business_id)
            sync_inventory = SyncAccountInventory(
                self._platform_port,
                SqlAccountRepository(session),
                SqlAdEntityRepository(session),
                self._clock,
            )
            ingest_daily = IngestDailyMetrics(SqlMetricFactRepository(session))
            compute_freshness = ComputeFreshness(SqlMetricFactRepository(session))
            freshness_repo = SqlFreshnessRepository(session)
            for account in accounts:
                await sync_inventory.execute(account.account_ref)
                snapshots = await self._platform_port.fetch_metrics(
                    MetricsRequest(
                        account_ref=account.account_ref,
                        window=window,
                        granularity=MetricGranularity.DAILY,
                    )
                )
                facts = [
                    _to_metric_fact(snapshot, account.timezone, self._clock.now())
                    for snapshot in snapshots
                    if snapshot.stat_hour is None
                ]
                if facts:
                    await ingest_daily.execute(IngestDailyMetricsRequest(facts=facts))
                    await self._record_freshness(
                        account.account_ref, facts, compute_freshness, freshness_repo
                    )
            await session.commit()

    async def _record_freshness(
        self,
        account_ref: AccountRef,
        facts: Sequence[MetricFact],
        compute_freshness: ComputeFreshness,
        freshness_repo: SqlFreshnessRepository,
    ) -> None:
        entity_refs_by_level: dict[EntityLevel, list[EntityRef]] = {}
        for fact in facts:
            entity_refs_by_level.setdefault(fact.entity_ref.level, []).append(fact.entity_ref)
        for level, entity_refs in entity_refs_by_level.items():
            try:
                freshness = await compute_freshness.execute(
                    ComputeFreshnessRequest(
                        platform_account_ref=str(account_ref),
                        entity_level=level,
                        entity_refs=entity_refs,
                        now=self._clock.now(),
                    )
                )
            except NoMetricsIngestedError:
                # Los hechos que se acaban de ingerir son de este mismo nivel
                # (vienen de esos `entity_refs`); esta rama no deberia
                # alcanzarse nunca en produccion -- se documenta en vez de
                # fallar el ciclo entero por una carrera improbable.
                logger.debug(
                    "freshness_skipped_no_metrics_ingested",
                    account_ref=str(account_ref),
                    entity_level=level.value,
                )
                continue
            await freshness_repo.save(freshness)


class LiveSignalStep:
    """`SignalEvaluationStepPort`: `DetectAnomalies`
    (`same_weekday_z` sobre la serie diaria real) mas `EvaluateEntitySignals`
    (puertas -> catalogo -> BUY/HOLD/SELL/EXIT).

    `SignalTargets.target_cpa_minor`/`target_roas` todavia no tienen fuente
    de economia real por entidad (`catalog`/`economics` exigen un
    `product_id` que `AdEntity` no lleva -- sigue siendo T157/futuro, fuera
    de esta lane): se usa `AdEntity.bid_target` cuando la cuenta lo
    configuro, y si no, 0 -- con target<=0 el catalogo entero de
    M05/M07/M09/G01/G04/G06 devuelve `None` (ver sus guardas), asi que la
    ausencia de objetivo nunca firma un BUY/SELL de mentira; solo M13
    (frecuencia/CTR, sin target) puede seguir disparando.

    `GateContext.median_lag_days` SI tiene fuente real (T156/T157):
    `_resolve_median_lag_by_platform` lee la curva de rezago mas madura del
    negocio para la plataforma de la cuenta (`SqlLagCurveRepository.
    get_most_mature_for_business`) y `resolve_median_lag_days` decide si
    esta calibrada; sin curva o con `sample_size` insuficiente, cae al
    `_DEFAULT_MEDIAN_LAG_DAYS` documentado. Los demas umbrales de puerta
    (`_DEFAULT_COOLDOWN`/`_DEFAULT_MIN_*`) si son constantes deliberadas de
    este momento del catalogo (profitability-engine.md: "hoy
    `target_cpa_minor` es constante")."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None:
        del now
        cycle_uuid = _cycle_uuid(cycle_id)
        as_of = self._clock.now()
        async with self._session_factory() as session:
            accounts = await SqlAccountRepository(session).list_for_observation(business_id)
            entity_repo = SqlAdEntityRepository(session)
            detect = DetectAnomalies(
                SqlDailySpendSeriesRepository(session),
                SqlAnomalyRepository(session, cycle_id=cycle_uuid),
            )
            evaluate_signals = EvaluateEntitySignals(
                SqlMetricWindowRepository(session),
                SqlSignalRepository(session, cycle_id=cycle_uuid),
            )
            median_lag_by_platform = await self._resolve_median_lag_by_platform(
                session, business_id, accounts
            )
            for account in accounts:
                median_lag_days = median_lag_by_platform[account.account_ref.platform]
                entities = await entity_repo.list_by_account(account.account_ref)
                for entity in entities:
                    if entity.entity_ref.level != EntityLevel.CAMPAIGN:
                        continue
                    await self._detect_one(detect, entity.entity_ref, as_of)
                    await evaluate_signals.execute(
                        _evaluate_signals_request(account, entity, as_of, median_lag_days)
                    )
            await session.commit()

    async def _resolve_median_lag_by_platform(
        self,
        session: AsyncSession,
        business_id: BusinessId,
        accounts: Sequence[PlatformAccount],
    ) -> dict[PlatformCode, int]:
        """T157: `median_lag_days` real por plataforma (la curva de mayor
        `sample_size` del negocio, `SqlLagCurveRepository.
        get_most_mature_for_business`), no la constante. Sin curva
        materializada todavia -- ninguna venta con calendario/CRM enlazado,
        o `ComputeLagCurve` (T156) sin correr para este negocio -- cae al
        valor por defecto documentado (`resolve_median_lag_days`,
        `is_calibrated=False`). Por producto exacto es T157/futuro
        (`AdEntity` no lleva `product_id` todavia, ver el docstring de la
        clase): esta es la mejor aproximacion real disponible hoy."""
        curves = SqlLagCurveRepository(session)
        platforms = {account.account_ref.platform for account in accounts}
        resolved: dict[PlatformCode, int] = {}
        for platform in platforms:
            curve = await curves.get_most_mature_for_business(
                business_id=business_id, platform=platform.value
            )
            resolution = resolve_median_lag_days(
                curve, default_median_lag_days=_DEFAULT_MEDIAN_LAG_DAYS
            )
            if not resolution.is_calibrated:
                logger.info(
                    "median_lag_days_not_calibrated",
                    business_id=str(business_id),
                    platform=platform.value,
                    sample_size=resolution.sample_size,
                )
            resolved[platform] = resolution.median_lag_days
        return resolved

    async def _detect_one(
        self, detect: DetectAnomalies, entity_ref: EntityRef, as_of: datetime
    ) -> None:
        try:
            await detect.execute(DetectAnomaliesRequest(entity_ref=entity_ref, as_of=as_of))
        except ZeroDivisionError:
            # Serie del mismo dia de la semana todavia vacia (entidad
            # nueva, sin 8 semanas de historial): nada que detectar, no es
            # un fallo del ciclo.
            logger.debug(
                "anomaly_detection_skipped_insufficient_history", entity_ref=str(entity_ref)
            )


def _evaluate_signals_request(
    account: PlatformAccount, entity: AdEntity, as_of: datetime, median_lag_days: int
) -> EvaluateEntitySignalsRequest:
    target_cpa_minor = (
        entity.bid_target.minor_units
        if entity.bid_target is not None and entity.bid_target.currency == account.currency
        else 0
    )
    return EvaluateEntitySignalsRequest(
        entity_ref=entity.entity_ref,
        targets=SignalTargets(
            currency=account.currency, target_cpa_minor=target_cpa_minor, target_roas=0.0
        ),
        gate_context=GateContext(
            learning_status=_LEARNING_STATUS_BY_STATE[entity.learning_state],
            last_change_at=None,
            cooldown=_DEFAULT_COOLDOWN,
            median_lag_days=median_lag_days,
            min_spend_multiple=_DEFAULT_MIN_SPEND_MULTIPLE,
            min_impressions=_DEFAULT_MIN_IMPRESSIONS,
            min_conversions=_DEFAULT_MIN_CONVERSIONS,
        ),
        as_of=as_of,
    )


def _to_metric_fact(
    snapshot: MetricFactSnapshot, account_timezone: str, ingested_at: datetime
) -> MetricFact:
    conversions = {
        ConversionKind(kind): count
        for kind, count in snapshot.conversions_by_kind.items()
        if kind in set(ConversionKind)
    }
    return MetricFact(
        entity_ref=snapshot.entity_ref,
        stat_date=snapshot.stat_date,
        stat_hour=snapshot.stat_hour,
        account_timezone=account_timezone,
        currency=snapshot.currency,
        spend_minor=snapshot.spend.minor_units,
        impressions=snapshot.impressions,
        clicks=snapshot.clicks,
        reach=snapshot.reach or 0,
        conversions=MappingProxyType(conversions),
        conversion_value_minor=(
            snapshot.conversion_value.minor_units if snapshot.conversion_value else 0
        ),
        video_views_3s=snapshot.video_views_3s or 0,
        video_views_75pct=snapshot.video_views_75pct or 0,
        search_lost_is_budget_pct=snapshot.search_lost_is_budget,
        search_lost_is_rank_pct=snapshot.search_lost_is_rank,
        ingested_at=ingested_at,
    )
