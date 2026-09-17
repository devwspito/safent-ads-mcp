"""`RefreshRegisteredEntityState` (003-entidades-creadas, item 2 del repaso
0.2.23): sube a ACTIVE y refresca el hash de la `AdEntity` que su propio
`CREATE_CAMPAIGN` ya registro PAUSED, sobre la MISMA fila."""

from __future__ import annotations

import uuid

import pytest

from safent_ads.accounts.application.errors import EntityNotFoundError
from safent_ads.accounts.application.refresh_registered_entity_state import (
    RefreshRegisteredEntityState,
    RefreshRegisteredEntityStateCommand,
)
from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAdEntityRepository
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_UUID = uuid.uuid4()
_CONNECTION_ID = uuid.uuid4()
_BUSINESS_ID = BusinessId(_BUSINESS_UUID)


def _account_ref() -> EntityRef:
    return EntityRef(
        platform=PlatformCode.META,
        level=EntityLevel.ACCOUNT,
        external_id="act-1",
        business_id=_BUSINESS_UUID,
        connection_id=_CONNECTION_ID,
    )


def _campaign_ref() -> EntityRef:
    return EntityRef(
        platform=PlatformCode.META,
        level=EntityLevel.CAMPAIGN,
        external_id="123456",
        business_id=_BUSINESS_UUID,
        connection_id=_CONNECTION_ID,
    )


def _paused_campaign() -> AdEntity:
    return AdEntity(
        business_id=_BUSINESS_ID,
        entity_ref=_campaign_ref(),
        parent_ref=_account_ref(),
        name="create_campaign (campaign)",
        status=AdEntityStatus.PAUSED,
        platform_state_hash=PlatformStateHash("a" * 64),
        is_controllable=True,
        learning_state=LearningState.NOT_APPLICABLE,
    )


async def test_flips_a_paused_campaign_to_active_with_the_confirmed_hash() -> None:
    entities = InMemoryAdEntityRepository([_paused_campaign()])
    use_case = RefreshRegisteredEntityState(entities)

    await use_case.execute(
        RefreshRegisteredEntityStateCommand(
            entity_ref=_campaign_ref(), confirmed_state_hash="b" * 64
        )
    )

    stored = await entities.get_by_ref(_campaign_ref())
    assert stored is not None
    assert stored.status is AdEntityStatus.ACTIVE
    assert stored.platform_state_hash.value == "b" * 64


async def test_fails_closed_when_the_campaign_was_never_registered() -> None:
    entities = InMemoryAdEntityRepository()
    use_case = RefreshRegisteredEntityState(entities)

    with pytest.raises(EntityNotFoundError):
        await use_case.execute(
            RefreshRegisteredEntityStateCommand(
                entity_ref=_campaign_ref(), confirmed_state_hash="b" * 64
            )
        )
