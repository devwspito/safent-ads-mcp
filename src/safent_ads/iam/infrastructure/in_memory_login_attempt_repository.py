"""Doble en memoria de `LoginAttemptRepository`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safent_ads.shared.clock import Clock, SystemClock


@dataclass(frozen=True, slots=True)
class _RecordedAttempt:
    email: str
    succeeded: bool
    ip_address: str | None
    attempted_at: datetime


class InMemoryLoginAttemptRepository:
    def __init__(self, clock: Clock | None = None) -> None:
        self._attempts: list[_RecordedAttempt] = []
        self._clock = clock or SystemClock()

    async def record(self, *, email: str, succeeded: bool, ip_address: str | None) -> None:
        now = self._clock.now()
        self._attempts.append(
            _RecordedAttempt(
                email=email, succeeded=succeeded, ip_address=ip_address, attempted_at=now
            )
        )

    async def count_recent_failures(
        self, *, email: str, ip_address: str | None, since: datetime
    ) -> int:
        return sum(
            1
            for attempt in self._attempts
            if attempt.email == email
            and attempt.ip_address == ip_address
            and not attempt.succeeded
            and attempt.attempted_at >= since
        )
