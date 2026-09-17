"""Never leave a half-alive worker after one of its required loops exits.

Partial business failures belong to the individual cycles. A loop itself
exiting is fatal: stop its siblings and let the process manager restart the
worker. Do not retry platform writes here; queue leases and broker idempotency
own recovery from an interrupted attempt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping


class WorkerTaskStoppedError(RuntimeError):
    """A required worker loop exited without a shutdown request."""


async def supervise_worker_tasks(
    tasks: Mapping[str, asyncio.Task[None]],
    *,
    stop_event: asyncio.Event,
    shutdown_timeout: float = 5.0,
) -> None:
    """Observe every loop, allow bounded graceful shutdown, then drain all tasks."""
    if not tasks:
        raise ValueError("worker supervision requires at least one task")
    stopping = asyncio.create_task(stop_event.wait(), name="worker-stop")
    try:
        done, _ = await asyncio.wait(
            [*tasks.values(), stopping], return_when=asyncio.FIRST_COMPLETED
        )
        for name, task in tasks.items():
            if task not in done:
                continue
            if task.cancelled():
                raise WorkerTaskStoppedError(f"worker loop cancelled: {name}")
            error = task.exception()
            if error is not None:
                raise WorkerTaskStoppedError(f"worker loop failed: {name}") from error
            if not stop_event.is_set():
                raise WorkerTaskStoppedError(f"worker loop stopped unexpectedly: {name}")
    finally:
        stop_event.set()
        stopping.cancel()
        # Let in-flight work persist before cancellation, but never wait forever.
        _, pending = await asyncio.wait(tasks.values(), timeout=shutdown_timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks.values(), stopping, return_exceptions=True)
    # Failure while finishing an in-flight operation is not a clean shutdown.
    for name, task in tasks.items():
        if not task.cancelled() and (error := task.exception()) is not None:
            raise WorkerTaskStoppedError(f"worker loop failed during shutdown: {name}") from error
