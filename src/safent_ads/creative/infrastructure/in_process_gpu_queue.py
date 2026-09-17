"""`InProcessGpuQueue` implementa `GpuLeasePort` (creative-port.md
§"Cola GPU"): "Un solo trabajo pesado a la vez... quedarse sin memoria
unificada cuelga la DGX, no mata el proceso" (threat-model.md C-29).

Solo los trabajos `JobWeight.HEAVY` compiten por la exclusion mutua: los
`LIGHT` (banners via Playwright, sin GPU) no tocan el candado. `asyncio.Lock`
despierta a los que esperan en orden FIFO (implementacion de referencia de
CPython), asi que no hace falta una cola explicita. La cancelacion la da
`asyncio.wait_for` de fabrica: si la tarea que espera se cancela, el candado
nunca se concede y el siguiente en la fila lo recibe."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from safent_ads.creative.domain.enums import JobWeight
from safent_ads.creative.domain.gpu_lease import GpuLease
from safent_ads.shared.clock import Clock
from safent_ads.shared.errors import InfrastructureError

_DEFAULT_LEASE_TTL_S = 600


class GpuLeaseTimeoutError(InfrastructureError):
    """No se obtuvo la GPU dentro de `timeout_s` (cola llena)."""


class UnknownGpuLeaseError(InfrastructureError):
    """`release` con un lease que esta cola no emitio o ya se libero."""


class InProcessGpuQueue:
    def __init__(self, clock: Clock, lease_ttl_s: int = _DEFAULT_LEASE_TTL_S) -> None:
        self._heavy_lock = asyncio.Lock()
        self._clock = clock
        self._lease_ttl_s = lease_ttl_s
        self._lease_weights: dict[uuid.UUID, JobWeight] = {}

    async def acquire(self, weight: JobWeight, timeout_s: int) -> GpuLease:
        if weight == JobWeight.HEAVY:
            await self._acquire_heavy_lock(timeout_s)
        return self._issue_lease(weight)

    async def _acquire_heavy_lock(self, timeout_s: int) -> None:
        try:
            await asyncio.wait_for(self._heavy_lock.acquire(), timeout=timeout_s)
        except TimeoutError as exc:
            raise GpuLeaseTimeoutError(
                f"timeout tras {timeout_s}s esperando un trabajo pesado libre"
            ) from exc

    def _issue_lease(self, weight: JobWeight) -> GpuLease:
        now = self._clock.now()
        lease = GpuLease(
            lease_id=uuid.uuid4(),
            acquired_at=now,
            expires_at=now + timedelta(seconds=self._lease_ttl_s),
        )
        self._lease_weights[lease.lease_id] = weight
        return lease

    async def release(self, lease: GpuLease) -> None:
        weight = self._lease_weights.pop(lease.lease_id, None)
        if weight is None:
            raise UnknownGpuLeaseError(str(lease.lease_id))
        if weight == JobWeight.HEAVY:
            self._heavy_lock.release()

    @property
    def is_heavy_slot_busy(self) -> bool:
        return self._heavy_lock.locked()
