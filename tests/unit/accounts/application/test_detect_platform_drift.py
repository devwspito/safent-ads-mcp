"""`DetectPlatformDrift`: marca `AdEntity` como `drifted` cuando el hash
remoto diverge del conocido, y publica `AdEntityDrifted` (T029, C-16)."""

from __future__ import annotations

import pytest

from safent_ads.accounts.application.detect_platform_drift import DetectPlatformDrift
from safent_ads.accounts.application.errors import EntityNotFoundError
from safent_ads.accounts.application.ports import EntityStateSnapshot
from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.events import AdEntityDrifted
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAdEntityRepository
from safent_ads.shared.clock import FixedClock
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode
from tests.unit.accounts.application.conftest import NOW, FakeAdsPlatformPort, FakeEventBus

_ENTITY_REF = EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "1")
_PARENT_REF = EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "acc-1")
_HASH_KNOWN = PlatformStateHash.compute({"status": "ACTIVE"})
_HASH_OBSERVED = PlatformStateHash.compute({"status": "PAUSED"})


def _entity() -> AdEntity:
    return AdEntity(
        business_id=BusinessId.new(),
        entity_ref=_ENTITY_REF,
        parent_ref=_PARENT_REF,
        name="Campana Otoño",
        status=AdEntityStatus.ACTIVE,
        platform_state_hash=_HASH_KNOWN,
        is_controllable=True,
    )


def _state_snapshot(canonical_state: dict[str, object]) -> EntityStateSnapshot:
    return EntityStateSnapshot(
        entity_ref=_ENTITY_REF,
        status=AdEntityStatus.ACTIVE,
        is_controllable=True,
        canonical_state=canonical_state,
        fetched_at=NOW,
    )


async def test_raises_when_entity_unknown() -> None:
    use_case = DetectPlatformDrift(
        FakeAdsPlatformPort(), InMemoryAdEntityRepository(), FakeEventBus(), FixedClock(NOW)
    )

    with pytest.raises(EntityNotFoundError):
        await use_case.execute(_ENTITY_REF)


async def test_no_drift_when_hash_matches() -> None:
    entity_repo = InMemoryAdEntityRepository([_entity()])
    port = FakeAdsPlatformPort(entity_states={_ENTITY_REF: _state_snapshot({"status": "ACTIVE"})})
    use_case = DetectPlatformDrift(port, entity_repo, FakeEventBus(), FixedClock(NOW))

    drifted = await use_case.execute(_ENTITY_REF)

    assert drifted is False
    stored = await entity_repo.get_by_ref(_ENTITY_REF)
    assert stored is not None
    assert stored.status == AdEntityStatus.ACTIVE


async def test_drift_marks_entity() -> None:
    entity_repo = InMemoryAdEntityRepository([_entity()])
    port = FakeAdsPlatformPort(entity_states={_ENTITY_REF: _state_snapshot({"status": "PAUSED"})})
    event_bus = FakeEventBus()
    use_case = DetectPlatformDrift(port, entity_repo, event_bus, FixedClock(NOW))

    drifted = await use_case.execute(_ENTITY_REF)

    assert drifted is True
    stored = await entity_repo.get_by_ref(_ENTITY_REF)
    assert stored is not None
    assert stored.status == AdEntityStatus.DRIFTED
    assert len(event_bus.published) == 1
    assert isinstance(event_bus.published[0], AdEntityDrifted)
