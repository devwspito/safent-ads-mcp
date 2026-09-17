"""Exercise the production worker wiring without network/platform credentials."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from safent_ads.composition import worker
from safent_ads.composition.settings import WorkerSettings
from safent_ads.orchestration.application.worker_supervisor import WorkerTaskStoppedError
from tests.unit.composition.factories import build_api_settings


@pytest.mark.parametrize("telegram_enabled", [False, True])
async def test_loop_failure_is_observed_by_production_wiring(
    monkeypatch: pytest.MonkeyPatch, telegram_enabled: bool
) -> None:
    settings = WorkerSettings(**build_api_settings(
        telegram_bot_token="123456:test-token" if telegram_enabled else "",
        telegram_owner_chat_ids=[123] if telegram_enabled else [],
        campaign_packages_enabled=True,
    ).model_dump())
    stop = asyncio.Event()
    container = Mock()
    monkeypatch.setattr(worker, "build_default_runtime", Mock())
    monkeypatch.setattr(worker, "run_forever", AsyncMock(side_effect=ConnectionError("db down")))

    async def until_stop(*args: object, **kwargs: object) -> None:
        await stop.wait()

    execution = AsyncMock(side_effect=until_stop)
    telegram = AsyncMock(side_effect=until_stop)
    package_publication = AsyncMock(side_effect=until_stop)
    monkeypatch.setattr(worker, "run_execution_forever", execution)
    monkeypatch.setattr(worker, "run_telegram_channel", telegram)
    monkeypatch.setattr(worker, "run_package_publications_forever", package_publication)
    with pytest.raises(WorkerTaskStoppedError, match="observation"):
        await worker._run_loops(container, settings, stop)
    execution.assert_awaited_once()
    package_publication.assert_awaited_once()
    assert telegram.await_count == int(telegram_enabled)
    assert stop.is_set()


@pytest.mark.parametrize(
    ("campaign_packages_enabled", "expect_started"), [(False, False), (True, True)]
)
async def test_package_publication_loop_is_gated_by_the_campaign_packages_flag(
    monkeypatch: pytest.MonkeyPatch, campaign_packages_enabled: bool, expect_started: bool
) -> None:
    """H2 (revision de codigo, saga de publicacion 2026-09-15): con el
    flag apagado no existe la saga que consume este bucle -- sondear cada
    5 s arriesgaria mover una fila de publicacion abierta de antes de
    apagarlo, sin operador vigilando esa superficie."""
    settings = WorkerSettings(
        **build_api_settings(campaign_packages_enabled=campaign_packages_enabled).model_dump()
    )
    stop = asyncio.Event()
    container = Mock()
    monkeypatch.setattr(worker, "build_default_runtime", Mock())

    async def until_stop(*args: object, **kwargs: object) -> None:
        await stop.wait()

    observation = AsyncMock(side_effect=until_stop)
    execution = AsyncMock(side_effect=until_stop)
    telegram = AsyncMock(side_effect=until_stop)
    package_publication = AsyncMock(side_effect=until_stop)
    monkeypatch.setattr(worker, "run_forever", observation)
    monkeypatch.setattr(worker, "run_execution_forever", execution)
    monkeypatch.setattr(worker, "run_telegram_channel", telegram)
    monkeypatch.setattr(worker, "run_package_publications_forever", package_publication)
    stop.set()

    await worker._run_loops(container, settings, stop)

    assert package_publication.await_count == int(expect_started)


async def test_startup_failure_releases_lock_and_container(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = WorkerSettings(**build_api_settings().model_dump())
    container = Mock(aclose=AsyncMock())
    lock = Mock(acquire=AsyncMock(return_value=True), release=AsyncMock())
    monkeypatch.setattr(worker, "configure_logging", Mock())
    monkeypatch.setattr(worker, "start_metrics_server", Mock())
    monkeypatch.setattr(worker.Container, "build", Mock(return_value=container))
    monkeypatch.setattr(worker, "SingleWorkerLock", Mock(return_value=lock))
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", Mock())
    monkeypatch.setattr(worker, "_run_loops", AsyncMock(side_effect=RuntimeError("startup failed")))
    with pytest.raises(RuntimeError, match="startup failed"):
        await worker.run(settings)
    lock.release.assert_awaited_once()
    container.aclose.assert_awaited_once()


async def test_release_failure_still_closes_container(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = WorkerSettings(**build_api_settings().model_dump())
    container = Mock(aclose=AsyncMock())
    lock = Mock(
        acquire=AsyncMock(return_value=True), release=AsyncMock(side_effect=ConnectionError)
    )
    monkeypatch.setattr(worker, "configure_logging", Mock())
    monkeypatch.setattr(worker, "start_metrics_server", Mock())
    monkeypatch.setattr(worker.Container, "build", Mock(return_value=container))
    monkeypatch.setattr(worker, "SingleWorkerLock", Mock(return_value=lock))
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", Mock())
    monkeypatch.setattr(worker, "_run_loops", AsyncMock())
    with pytest.raises(ConnectionError):
        await worker.run(settings)
    container.aclose.assert_awaited_once()
