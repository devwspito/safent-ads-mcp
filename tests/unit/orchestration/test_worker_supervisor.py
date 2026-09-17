"""A process that is alive but no longer observing/executing is a failure."""

import asyncio

import pytest

from safent_ads.orchestration.application.worker_supervisor import (
    WorkerTaskStoppedError,
    supervise_worker_tasks,
)


@pytest.mark.parametrize("failed_loop", ["observation", "execution", "telegram"])
async def test_loop_failure_stops_siblings_and_propagates(failed_loop: str) -> None:
    stop = asyncio.Event()
    sibling_finished = asyncio.Event()

    async def broken() -> None:
        raise ConnectionError("database unavailable")

    async def sibling() -> None:
        await stop.wait()
        sibling_finished.set()

    tasks = {
        failed_loop: asyncio.create_task(broken()),
        "sibling": asyncio.create_task(sibling()),
    }
    with pytest.raises(WorkerTaskStoppedError, match=failed_loop) as raised:
        await supervise_worker_tasks(tasks, stop_event=stop)
    assert isinstance(raised.value.__cause__, ConnectionError)
    assert stop.is_set()
    assert sibling_finished.is_set()
    assert all(task.done() for task in tasks.values())


async def test_unexpected_clean_return_is_not_success() -> None:
    async def stopped() -> None:
        return

    with pytest.raises(WorkerTaskStoppedError, match="stopped unexpectedly"):
        await supervise_worker_tasks(
            {"observation": asyncio.create_task(stopped())}, stop_event=asyncio.Event()
        )


async def test_cancelled_required_loop_is_not_ignored() -> None:
    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(WorkerTaskStoppedError, match="cancelled: execution"):
        await supervise_worker_tasks(
            {"execution": asyncio.create_task(cancelled())}, stop_event=asyncio.Event()
        )


async def test_requested_shutdown_allows_inflight_result_to_finish() -> None:
    stop = asyncio.Event()
    persisted = asyncio.Event()

    async def inflight() -> None:
        await stop.wait()
        persisted.set()

    task = asyncio.create_task(inflight())
    stop.set()
    await supervise_worker_tasks({"execution": task}, stop_event=stop)
    assert persisted.is_set()
    assert not task.cancelled()


async def test_shutdown_cancels_and_drains_a_stuck_loop() -> None:
    stop = asyncio.Event()
    entered = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def stuck() -> None:
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    task = asyncio.create_task(stuck())
    await entered.wait()
    stop.set()
    await supervise_worker_tasks({"execution": task}, stop_event=stop, shutdown_timeout=0)
    assert task.cancelled()
    assert cleaned_up.is_set()


async def test_cancelling_supervisor_stops_children_without_orphans() -> None:
    stop = asyncio.Event()
    entered = asyncio.Event()

    async def sibling() -> None:
        entered.set()
        await stop.wait()

    child = asyncio.create_task(sibling())
    supervisor = asyncio.create_task(supervise_worker_tasks({"execution": child}, stop_event=stop))
    await entered.wait()
    supervisor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await supervisor
    assert stop.is_set()
    assert child.done()


async def test_simultaneous_stop_does_not_mask_a_loop_failure() -> None:
    stop = asyncio.Event()

    async def broken() -> None:
        stop.set()
        raise RuntimeError("cannot persist outcome")

    with pytest.raises(WorkerTaskStoppedError, match="execution"):
        await supervise_worker_tasks({"execution": asyncio.create_task(broken())}, stop_event=stop)


async def test_failure_during_graceful_shutdown_is_not_swallowed() -> None:
    stop = asyncio.Event()

    async def inflight() -> None:
        await stop.wait()
        # Force the exception after the supervisor has observed the stop event.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        raise RuntimeError("failed final commit")

    task = asyncio.create_task(inflight())
    stop.set()
    with pytest.raises(WorkerTaskStoppedError, match="execution"):
        await supervise_worker_tasks({"execution": task}, stop_event=stop)
