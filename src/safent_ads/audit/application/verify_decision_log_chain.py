"""`VerifyDecisionLogChain`: usa `ChainVerifier` (dominio, puro) sobre las
filas que trae `DecisionLogRepository.stream_for_verification` (threat-model
C-19/C-20; `GET /decision-log/verify` de contracts/rest-api.md; paso 5 de
quickstart.md §4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safent_ads.audit.application.ports import DecisionLogRepository
from safent_ads.audit.domain.chain import ChainVerifier
from safent_ads.shared.clock import Clock


@dataclass(frozen=True, slots=True)
class ChainVerificationReport:
    chain_ok: bool
    verified_through_seq: int | None
    checked_at: datetime


class VerifyDecisionLogChain:
    def __init__(
        self, repository: DecisionLogRepository, verifier: ChainVerifier, clock: Clock
    ) -> None:
        self._repository = repository
        self._verifier = verifier
        self._clock = clock

    async def execute(self) -> ChainVerificationReport:
        rows = [row async for row in self._repository.stream_for_verification()]
        result = self._verifier.verify(rows)
        return ChainVerificationReport(
            chain_ok=result.chain_ok,
            verified_through_seq=result.verified_through_seq,
            checked_at=self._clock.now(),
        )
