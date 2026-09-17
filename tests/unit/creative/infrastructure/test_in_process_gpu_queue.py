"""`InProcessGpuQueue`: un solo trabajo pesado a la vez, FIFO, timeout y
cancelable (creative-port.md §"Cola GPU"; T102 `test_single_heavy_job`)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from safent_ads.creative.domain.enums import JobWeight
from safent_ads.creative.infrastructure.in_process_gpu_queue import (
    GpuLeaseTimeoutError,
    InProcessGpuQueue,
    UnknownGpuLeaseError,
)
from safent_ads.shared.clock import FixedClock


def _queue() -> InProcessGpuQueue:
    return InProcessGpuQueue(FixedClock(datetime.now(UTC)))


def test_single_heavy_job_only_one_runs_at_a_time() -> None:
    async def _run() -> list[str]:
        queue = _queue()
        events: list[str] = []

        async def heavy_job(name: str) -> None:
            lease = await queue.acquire(JobWeight.HEAVY, timeout_s=5)
            events.append(f"start-{name}")
            await asyncio.sleep(0.02)
            events.append(f"end-{name}")
            await queue.release(lease)

        await asyncio.gather(heavy_job("a"), heavy_job("b"))
        return events

    events = asyncio.run(_run())

    # Un job pesado termina antes de que el otro empiece: nunca se solapan.
    first_pair, second_pair = events[:2], events[2:]
    assert first_pair[0].startswith("start-")
    assert first_pair[1] == f"end-{first_pair[0].removeprefix('start-')}"
    assert second_pair[0].startswith("start-")


def test_light_jobs_do_not_wait_behind_heavy_ones() -> None:
    async def _run() -> list[str]:
        queue = _queue()
        events: list[str] = []
        heavy_lease = await queue.acquire(JobWeight.HEAVY, timeout_s=5)
        events.append("heavy-acquired")

        light_lease = await asyncio.wait_for(queue.acquire(JobWeight.LIGHT, timeout_s=1), 0.5)
        events.append("light-acquired")

        await queue.release(light_lease)
        await queue.release(heavy_lease)
        return events

    events = asyncio.run(_run())

    assert events == ["heavy-acquired", "light-acquired"]


def test_second_heavy_job_times_out_while_first_holds_the_lease() -> None:
    async def _run() -> None:
        queue = _queue()
        await queue.acquire(JobWeight.HEAVY, timeout_s=5)

        with pytest.raises(GpuLeaseTimeoutError):
            await queue.acquire(JobWeight.HEAVY, timeout_s=0.05)

    asyncio.run(_run())


def test_release_unknown_lease_raises() -> None:
    async def _run() -> None:
        queue = _queue()
        other_queue = _queue()
        foreign_lease = await other_queue.acquire(JobWeight.HEAVY, timeout_s=1)

        with pytest.raises(UnknownGpuLeaseError):
            await queue.release(foreign_lease)

    asyncio.run(_run())


def test_release_frees_the_slot_for_the_next_heavy_job() -> None:
    async def _run() -> bool:
        queue = _queue()
        lease = await queue.acquire(JobWeight.HEAVY, timeout_s=1)
        assert queue.is_heavy_slot_busy
        await queue.release(lease)
        assert not queue.is_heavy_slot_busy

        second_lease = await asyncio.wait_for(queue.acquire(JobWeight.HEAVY, timeout_s=1), 0.5)
        await queue.release(second_lease)
        return True

    assert asyncio.run(_run())


def test_cancelling_a_waiter_lets_the_next_one_through() -> None:
    async def _run() -> str:
        queue = _queue()
        held = await queue.acquire(JobWeight.HEAVY, timeout_s=1)

        waiter = asyncio.ensure_future(queue.acquire(JobWeight.HEAVY, timeout_s=5))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        await queue.release(held)
        next_lease = await asyncio.wait_for(queue.acquire(JobWeight.HEAVY, timeout_s=1), 0.5)
        return str(next_lease.lease_id)

    assert asyncio.run(_run())
