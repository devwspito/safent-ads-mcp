"""`RegisterCreatedEntity` (003-entidades-creadas): registro sincrono, desde
el recibo de un paso de creacion, de la `AdEntity` que
`proposals_entity_exists()` (0027) exige antes de aceptar la `Proposal` del
hijo de ese paso."""

from __future__ import annotations

import uuid
from dataclasses import replace

from safent_ads.accounts.application.register_created_entity import (
    RegisterCreatedEntity,
    RegisterCreatedEntityCommand,
)
from safent_ads.accounts.domain.ad_entity import AdEntityStatus
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.testing.in_memory_repositories import InMemoryAdEntityRepository
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_BUSINESS_UUID = uuid.uuid4()
_CONNECTION_ID = uuid.uuid4()
_BUSINESS_ID = BusinessId(_BUSINESS_UUID)
_STATE_HASH = "a" * 64


def _account_ref() -> EntityRef:
    return EntityRef(
        platform=PlatformCode.META,
        level=EntityLevel.ACCOUNT,
        external_id="act-1",
        business_id=_BUSINESS_UUID,
        connection_id=_CONNECTION_ID,
    )


def _campaign_ref(external_id: str = "123456") -> EntityRef:
    return EntityRef(
        platform=PlatformCode.META,
        level=EntityLevel.CAMPAIGN,
        external_id=external_id,
        business_id=_BUSINESS_UUID,
        connection_id=_CONNECTION_ID,
    )


def _command(**overrides: object) -> RegisterCreatedEntityCommand:
    defaults: dict[str, object] = {
        "business_id": _BUSINESS_ID,
        "entity_ref": _campaign_ref(),
        "parent_ref": _account_ref(),
        "name": "create_campaign (campaign)",
        # `platform_completeness` firma "PAUSED" para los tres pasos que
        # crean (packages/domain/platform_completeness.py): la plataforma
        # nunca crea una entidad ya activa -- el default del helper de test
        # sigue lo que `RunPackagePublication` pasa de verdad, nunca un
        # `ACTIVE` que el caso de uso ya no decide por si solo (item 1,
        # repaso 0.2.23).
        "status": AdEntityStatus.PAUSED,
        "platform_state_hash": _STATE_HASH,
    }
    defaults.update(overrides)
    return RegisterCreatedEntityCommand(**defaults)  # type: ignore[arg-type]


async def test_registers_a_new_entity_with_its_parent_and_hash() -> None:
    entities = InMemoryAdEntityRepository()
    use_case = RegisterCreatedEntity(entities)

    await use_case.execute(_command())

    stored = await entities.get_by_ref(_campaign_ref())
    assert stored is not None
    assert stored.business_id == _BUSINESS_ID
    assert stored.parent_ref == _account_ref()
    assert stored.platform_state_hash.value == _STATE_HASH
    assert stored.status is AdEntityStatus.PAUSED
    assert stored.is_controllable is True
    assert stored.learning_state is LearningState.NOT_APPLICABLE


async def test_registers_the_status_the_caller_passes_not_a_hardcoded_one() -> None:
    # Regresion (item 1, repaso 0.2.23): `RegisterCreatedEntity` hardcodeaba
    # `AdEntityStatus.ACTIVE` sin mirar el `command` -- una campaña recien
    # creada (siempre PAUSED, `platform_completeness`) quedaba marcada
    # ACTIVE, y `entity_lifecycle_actions`/`apply_defensive_action` la
    # trataban como si ya se pudiera pausar/reanudar de verdad.
    entities = InMemoryAdEntityRepository()
    use_case = RegisterCreatedEntity(entities)

    await use_case.execute(_command(status=AdEntityStatus.ACTIVE))

    stored = await entities.get_by_ref(_campaign_ref())
    assert stored is not None
    assert stored.status is AdEntityStatus.ACTIVE


async def test_is_idempotent_for_an_already_registered_entity() -> None:
    entities = InMemoryAdEntityRepository()
    use_case = RegisterCreatedEntity(entities)
    await use_case.execute(_command())

    # Un recibo repetido (reconciliacion tras una reanudacion) nunca
    # reescribe la entidad ya registrada -- solo el sync periodico
    # actualiza campos ya conocidos.
    await use_case.execute(
        _command(name="otro nombre", platform_state_hash="b" * 64)
    )

    stored = await entities.get_by_ref(_campaign_ref())
    assert stored is not None
    assert stored.name == "create_campaign (campaign)"
    assert stored.platform_state_hash.value == _STATE_HASH


async def test_registers_a_child_against_its_just_registered_parent() -> None:
    entities = InMemoryAdEntityRepository()
    use_case = RegisterCreatedEntity(entities)
    await use_case.execute(_command())
    ad_set_ref = EntityRef(
        platform=PlatformCode.META,
        level=EntityLevel.AD_SET,
        external_id="as-1",
        business_id=_BUSINESS_UUID,
        connection_id=_CONNECTION_ID,
    )

    await use_case.execute(
        replace(_command(), entity_ref=ad_set_ref, parent_ref=_campaign_ref(), name="ad_set")
    )

    stored = await entities.get_by_ref(ad_set_ref)
    assert stored is not None
    assert stored.parent_ref == _campaign_ref()
