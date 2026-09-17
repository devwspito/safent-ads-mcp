"""Dobles en memoria de `BusinessListingPort`/`IngestionStepPort`/
`SignalEvaluationStepPort`/`NotificationTargetsPort` (T047)."""

from __future__ import annotations

from datetime import datetime

from safent_ads.orchestration.application.ports import NotificationTarget
from safent_ads.shared.ids import BusinessId


class FakeBusinessListing:
    def __init__(self, business_ids: list[BusinessId]) -> None:
        self._business_ids = business_ids

    async def list_active_business_ids(self) -> list[BusinessId]:
        return list(self._business_ids)


class FakeStep:
    """Sirve de `IngestionStepPort` y `SignalEvaluationStepPort` (misma
    forma). `fail_for` fuerza un fallo (siempre, para probar el reintento
    agotado) en negocios concretos."""

    def __init__(self, *, fail_for: frozenset[BusinessId] = frozenset()) -> None:
        self.calls: list[tuple[BusinessId, str, datetime]] = []
        self._fail_for = fail_for

    async def run(self, business_id: BusinessId, cycle_id: str, now: datetime) -> None:
        self.calls.append((business_id, cycle_id, now))
        if business_id in self._fail_for:
            raise RuntimeError(f"fallo simulado para {business_id}")


class FakeNotificationTargets:
    def __init__(self, targets: list[NotificationTarget]) -> None:
        self._targets = targets

    async def list_notification_targets(self) -> list[NotificationTarget]:
        return list(self._targets)
