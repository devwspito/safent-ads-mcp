"""Doble en memoria de `BrandClaimsDecisionRecorder` para tests de
`UpdateBrandClaims`/`build_brand_router` sin depender de `audit`/Postgres."""

from __future__ import annotations

from dataclasses import dataclass

from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, slots=True)
class RecordedBrandClaimsDecision:
    business_id: BusinessId
    actor_email: str


class RecordingBrandClaimsDecisionRecorder:
    def __init__(self) -> None:
        self.recorded: list[RecordedBrandClaimsDecision] = []

    async def record(self, *, business_id: BusinessId, actor_email: str) -> None:
        self.recorded.append(
            RecordedBrandClaimsDecision(business_id=business_id, actor_email=actor_email)
        )
