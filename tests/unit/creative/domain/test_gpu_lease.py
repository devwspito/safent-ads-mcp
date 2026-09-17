from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from safent_ads.creative.domain.gpu_lease import GpuLease


def test_is_expired_at_before_expiry_is_false() -> None:
    now = datetime.now(UTC)
    lease = GpuLease(lease_id=uuid.uuid4(), acquired_at=now, expires_at=now + timedelta(minutes=5))

    assert not lease.is_expired_at(now + timedelta(minutes=1))


def test_is_expired_at_after_expiry_is_true() -> None:
    now = datetime.now(UTC)
    lease = GpuLease(lease_id=uuid.uuid4(), acquired_at=now, expires_at=now + timedelta(minutes=5))

    assert lease.is_expired_at(now + timedelta(minutes=10))
