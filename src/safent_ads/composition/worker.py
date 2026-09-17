"""Entrypoint de `ads-worker`: `IngestionCycle`/`SignalCycle`/`RuleCycle`/
`NotificationCycle` (15/60 min segun horario activo,
`orchestration/presentation/worker_schedule.py::run_forever`) mas
`ExecutionCycle` en su propio bucle de 30 s (`run_execution_forever`,
plan.md §7). Mantenimiento, oportunidades y salud de credenciales tambien
forman parte de la vuelta de observacion. Los bucles estan supervisados:
si uno termina inesperadamente el proceso falla para que compose lo reinicie.

Eleccion de scheduler (pyproject.toml no incluye `apscheduler`): las
cadencias de plan.md §7 son fijas y conocidas en tiempo de diseno (15/60 min,
30 s) y cada ciclo ya debe ser idempotente y sin estado compartido por
contrato propio del dominio. Un `asyncio.sleep` en bucle cubre eso sin
anadir persistencia de jobs, hilos de reloj wall-clock ni un scheduler
store que este sistema no necesita. Si en una fase posterior aparece la
necesidad real de recuperacion tras caida con jobs perdidos o coalescing
complejo, se reevalua entonces (YAGNI); por ahora la complejidad de
APScheduler no se paga."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
)
from safent_ads.accounts.application.register_created_entity import RegisterCreatedEntity
from safent_ads.accounts.infrastructure.oauth_broker_client import OAuthBrokerSocketClient
from safent_ads.accounts.infrastructure.sql_repositories import SqlAdEntityRepository
from safent_ads.composition.container import Container, ExecutionUseCases
from safent_ads.composition.settings import WorkerSettings
from safent_ads.creative.infrastructure.local_asset_storage import LocalAssetStorage
from safent_ads.creative.infrastructure.sql_repositories import SqlCreativeAssetRepository
from safent_ads.execution.infrastructure.sql_spend_ledger import SqlSpendLedger
from safent_ads.logging_setup import configure_logging
from safent_ads.notifications.domain.value_objects import ActiveHoursWindow
from safent_ads.notifications.infrastructure.telegram_channel import run_telegram_channel
from safent_ads.observability.server import start_metrics_server
from safent_ads.orchestration.application.worker_supervisor import supervise_worker_tasks
from safent_ads.orchestration.infrastructure.runtime import build_default_runtime
from safent_ads.orchestration.infrastructure.single_worker_lock import (
    SingleWorkerLock,
    WorkerAlreadyRunningError,
)
from safent_ads.orchestration.presentation.worker_schedule import run_execution_forever, run_forever
from safent_ads.packages.application.errors import PackageApplicationError
from safent_ads.packages.application.ports import PackagePublicationRepository
from safent_ads.packages.application.run_package_publication import RunPackagePublication
from safent_ads.packages.domain.errors import PackageDomainError
from safent_ads.packages.infrastructure.chokepoint_step_executor import ChokepointStepExecutor
from safent_ads.packages.infrastructure.creative_asset_lookup import PackageCreativeAssetLookup
from safent_ads.packages.infrastructure.sql_package_repository import SqlCampaignPackageRepository
from safent_ads.packages.infrastructure.sql_package_step_repository import (
    SqlPackageStepRepository,
)
from safent_ads.packages.infrastructure.sql_publication_repository import (
    SqlPackagePublicationRepository,
)
from safent_ads.shared.clock import Clock
from safent_ads.shared.crypto.hkdf import derive_key

logger = structlog.get_logger(__name__)

# T127: mismo puerto fijo que `composition/app.py`/`composition/broker.py`
# -- procesos y contenedores/netns distintos, sin colision real -- siempre
# en loopback (`observability/server.py`), nunca publicado en compose.
_METRICS_PORT = 9410

# 003-paquete-de-campana (T029): un `RunPackagePublication` por publicacion
# abierta y por ciclo, junto a `ExecutionChokepoint.run_once` -- mismo
# espiritu que `run_execution_forever` pero con su propio bucle: la gracia
# de 45 s y el ritmo de una saga de hasta 29 pasos piden una cadencia mas
# corta que los 30 s de la cola generica, sin compartir su intervalo.
_PACKAGE_PUBLICATION_INTERVAL_SECONDS = 5
_CREATIVE_PREVIEW_SIGNING_KEY_INFO = b"safent-ads/creative-preview/v1"
# `ADS_CREATIVE_ASSET_STORAGE_DIR` vive en `ApiSettings`
# (`composition/settings.py`, fuera de esta rama de trabajo) -- el worker
# lee la MISMA variable de entorno directamente, con el mismo valor por
# defecto que `composition/app.py::_build_creative_asset_store`, en vez de
# duplicar el campo en una clase de settings que no le corresponde tocar
# aqui.
_DEFAULT_CREATIVE_ASSET_STORAGE_DIR = "data/creative-assets"


def _execution_use_cases_factory(
    container: Container,
) -> Callable[[AsyncSession], ExecutionUseCases]:
    """`orchestration.infrastructure.rule_step.ExecutionUseCasesFactory`
    espera exactamente `(session) -> WorkerExecutionUseCases`;
    `Container.build_execution_use_cases` tiene ademas `brake_actor`
    opcional, que orchestration no conoce (no le hace falta: ni
    `RuleCycle` ni `ExecutionCycle` pulsan el freno)."""

    def factory(session: AsyncSession) -> ExecutionUseCases:
        return container.build_execution_use_cases(session)

    return factory


def _package_creative_asset_store(container: Container) -> LocalAssetStorage:
    storage_dir = Path(
        os.environ.get("ADS_CREATIVE_ASSET_STORAGE_DIR", _DEFAULT_CREATIVE_ASSET_STORAGE_DIR)
    )
    return LocalAssetStorage(
        storage_dir,
        signing_key=derive_key(
            container.settings.session_secret.get_secret_value().encode(),
            _CREATIVE_PREVIEW_SIGNING_KEY_INFO,
        ),
        clock=container.clock,
    )


def _build_run_package_publication(
    container: Container, session: AsyncSession, asset_store: LocalAssetStorage
) -> RunPackagePublication:
    """Un `ChokepointStepExecutor` fresco por sesion, sobre el MISMO
    `ExecutionChokepoint`/freno/guardarrailes que cualquier propuesta
    aprobada (`Container.build_execution_use_cases`) -- ningun camino
    nuevo de escritura, solo un llamador nuevo del ya existente."""
    execution_use_cases = container.build_execution_use_cases(session)
    spend_ledger = SqlSpendLedger(session, container.clock, execution_use_cases.execution_queue)
    step_executor = ChokepointStepExecutor(
        proposals=execution_use_cases.proposals,
        authorizations=execution_use_cases.authorizations,
        propose_action=execution_use_cases.propose_action,
        execution_queue=execution_use_cases.execution_queue,
        chokepoint=execution_use_cases.chokepoint,
        guardrail_evaluator=execution_use_cases.guardrail_evaluator,
        guardrail_sets=execution_use_cases.guardrail_sets,
        spend_ledger=spend_ledger,
        platform=container.ads_platform_port,
        creative_bytes=PackageCreativeAssetLookup(SqlCreativeAssetRepository(session), asset_store),
        signer=container.approval_key_pair.signer,
        clock=container.clock,
    )
    return RunPackagePublication(
        packages=SqlCampaignPackageRepository(session),
        publications=SqlPackagePublicationRepository(session),
        steps=SqlPackageStepRepository(session),
        step_executor=step_executor,
        brakes=execution_use_cases.brakes,
        entity_registration=RegisterCreatedEntity(SqlAdEntityRepository(session)),
        entity_activation_refresh=RefreshRegisteredEntityState(SqlAdEntityRepository(session)),
        clock=container.clock,
        enabled_google_channels=container.settings.google_channels_enabled,
    )


_TERMINAL_PUBLICATION_STATES = frozenset({"completed", "halted"})


async def _halt_after_internal_error(
    publications: PackagePublicationRepository, publication_id: str, clock: Clock
) -> None:
    """M2 (repaso 0.2.23): un `PackageDomainError` escapando de
    `RunPackagePublication.execute` significa que el propio agregado
    rechazo la transicion -- reintentar el MISMO paso cada 5 s
    (`_PACKAGE_PUBLICATION_INTERVAL_SECONDS`) repetiria el mismo fallo para
    siempre. Se para la publicacion con una razon limpia, igual que
    cualquier otro halt (contracts/api.md): el dueño la ve y decide, en vez
    de que el worker entero se caiga por ella (ver `_run_package_
    publications_once`)."""
    record = await publications.get_by_id(publication_id)
    if record is None or record.state in _TERMINAL_PUBLICATION_STATES:
        return
    await publications.advance(
        publication_id,
        cursor=record.cursor,
        state="halted",
        halt_reason="package_publication_internal_error",
        failed_step_index=None,
        finished_at=clock.now(),
    )


async def _run_package_publications_once(
    container: Container, asset_store: LocalAssetStorage
) -> None:
    async with container.session_factory() as session:
        publications = SqlPackagePublicationRepository(session)
        open_publication_ids = await publications.list_open()
        for publication_id in open_publication_ids:
            use_case = _build_run_package_publication(container, session, asset_store)
            try:
                await use_case.execute(publication_id)
                await session.commit()
            except PackageApplicationError:
                logger.exception("package_publication_step_failed", publication_id=publication_id)
                await session.rollback()
                continue
            except Exception as exc:  # noqa: BLE001 - el bucle del worker nunca muere por UNA publicacion
                # M2 (repaso 0.2.23): `supervise_worker_tasks` trata CUALQUIER
                # excepcion sin capturar de un ciclo como fatal para TODOS los
                # ciclos (observacion, ejecucion, telegram) -- no solo el de
                # paquetes. Un invariante de dominio violado, un error
                # transitorio de la base o un OSError del almacen de activos
                # (revision final 0.2.23) PARA UNA publicacion nunca debe
                # tirar el worker entero; el resto de publicaciones abiertas
                # siguen su ciclo con normalidad en la siguiente vuelta.
                logger.exception(
                    "package_publication_unexpected_error",
                    publication_id=publication_id,
                    error_type=type(exc).__name__,
                )
                await session.rollback()
                if isinstance(exc, PackageDomainError):
                    await _halt_after_internal_error(publications, publication_id, container.clock)
                    await session.commit()
                continue


async def run_package_publications_forever(
    container: Container, *, stop_event: asyncio.Event
) -> None:
    """`003-paquete-de-campana` T029: un paso por publicacion abierta, por
    ciclo -- espejo de `run_execution_forever`, cadencia propia (mas corta
    que los 30 s de la cola generica: FR-08's 45 s de gracia y el objetivo
    de <60 s de SC-02 piden un tick mas fino)."""
    asset_store = _package_creative_asset_store(container)
    while not stop_event.is_set():
        await _run_package_publications_once(container, asset_store)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_PACKAGE_PUBLICATION_INTERVAL_SECONDS)
        except TimeoutError:
            continue


async def run(settings: WorkerSettings | None = None) -> None:
    configure_logging()
    start_metrics_server(_METRICS_PORT)
    resolved_settings = settings or WorkerSettings()  # type: ignore[call-arg]
    container = Container.build(resolved_settings)
    logger.info("ads_worker_starting")
    # Ver composition/app.py::create_app: mismo aviso, mismo criterio de
    # "vacio = desactivado" que `_build_messenger`/`run_telegram_channel`
    # ya aplicaban mas abajo en este mismo arranque.
    if not resolved_settings.telegram_bot_token.get_secret_value() or (
        not resolved_settings.telegram_owner_chat_ids
    ):
        logger.warning("telegram_not_configured")

    # threat-model.md C-15/C-17, security review F2/F3 nit 4: `compose.yaml`
    # fija una sola replica de `ads-worker`, pero eso no es un invariante que
    # el propio proceso pueda hacer cumplir por si solo -- un escalado manual
    # o un segundo despliegue contra el mismo Postgres lo incumplirian en
    # silencio. Se niega a arrancar ciclos si otro proceso ya tiene el
    # advisory lock, en vez de competir por la cola sin lock por cuenta.
    worker_lock = SingleWorkerLock(container.engine)
    if not await worker_lock.acquire():
        logger.error("ads_worker_refused_second_instance")
        await container.aclose()
        raise WorkerAlreadyRunningError(
            "otro proceso ads-worker ya tiene el advisory lock de ciclos"
        )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await _run_loops(container, resolved_settings, stop_event)
    finally:
        try:
            await worker_lock.release()
        finally:
            await container.aclose()
        logger.info("ads_worker_stopped")


async def _run_loops(
    container: Container, resolved_settings: WorkerSettings, stop_event: asyncio.Event
) -> None:
    active_hours = ActiveHoursWindow.parse(
        resolved_settings.active_hours, tz_name=resolved_settings.timezone
    )
    runtime = build_default_runtime(
        session_factory=container.session_factory,
        platform_port=container.ads_platform_port,
        oauth_broker=OAuthBrokerSocketClient(resolved_settings.broker_socket_path),
        active_hours=active_hours,
        execution_use_cases_factory=_execution_use_cases_factory(container),
        telegram_bot_token=resolved_settings.telegram_bot_token.get_secret_value() or None,
        telegram_owner_chat_ids=tuple(resolved_settings.telegram_owner_chat_ids),
        digest_hour_default=resolved_settings.digest_hour_default,
        clock=container.clock,
        id_generator=container.id_generator,
    )
    cycles_task = asyncio.create_task(
        run_forever(
            runtime, active_hours=active_hours, clock=container.clock, stop_event=stop_event
        )
    )
    execution_task = asyncio.create_task(run_execution_forever(runtime, stop_event=stop_event))
    # contracts/telegram.md (US3): el bot de aprobacion vive en este mismo
    # proceso, no en `ads-api` -- ver el docstring de `run_telegram_channel`.
    tasks = {
        "observation": cycles_task,
        "execution": execution_task,
    }
    # H2 (revision de codigo, saga de publicacion 2026-09-15):
    # `ADS_CAMPAIGN_PACKAGES_ENABLED` (mismo flag que gatea `/api/v1/
    # packages/**` en `composition/app.py`) tambien gatea este bucle -- con
    # el flag apagado no existe la saga de publicacion que lo consume, asi
    # que sondear cada 5 s (`_PACKAGE_PUBLICATION_INTERVAL_SECONDS`) solo
    # arriesgaria mover una fila de publicacion que quedo abierta de antes
    # de apagar el flag, sin ningun operador vigilando esa superficie.
    if resolved_settings.campaign_packages_enabled:
        tasks["package_publication"] = asyncio.create_task(
            run_package_publications_forever(container, stop_event=stop_event)
        )
    # An unconfigured optional channel returns immediately; it is not a loop.
    if (
        resolved_settings.telegram_bot_token.get_secret_value()
        and resolved_settings.telegram_owner_chat_ids
    ):
        tasks["telegram"] = asyncio.create_task(
            run_telegram_channel(
                session_factory=container.session_factory,
                execution_use_cases_factory=_execution_use_cases_factory(container),
                bot_token=resolved_settings.telegram_bot_token.get_secret_value() or None,
                owner_chat_ids=tuple(resolved_settings.telegram_owner_chat_ids),
                clock=container.clock,
                stop_event=stop_event,
            )
        )
    await supervise_worker_tasks(tasks, stop_event=stop_event)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
