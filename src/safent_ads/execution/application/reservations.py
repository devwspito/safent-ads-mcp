"""A committed reservation survives worker loss and authorization expiry."""

from typing import Protocol

from safent_ads.execution.domain.execution_attempt import ExecutionAttempt
from safent_ads.proposals.domain.proposal import ProposedDiff


class ExecutionReservations(Protocol):
    async def reserve(self, attempt: ExecutionAttempt, effective: ProposedDiff) -> None: ...

    async def get(self, attempt: ExecutionAttempt) -> ProposedDiff | None: ...

    async def resolve(self, attempt: ExecutionAttempt, *, applied: bool) -> None: ...
