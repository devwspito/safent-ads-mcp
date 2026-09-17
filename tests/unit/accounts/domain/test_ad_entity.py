"""Invariantes de jerarquia y transiciones de `AdEntity` (data-model.md)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from safent_ads.accounts.domain.ad_entity import AdEntity, AdEntityStatus
from safent_ads.accounts.domain.budget import Budget, BudgetKind
from safent_ads.accounts.domain.errors import (
    InvalidEntityHierarchyError,
    InvalidStateTransitionError,
)
from safent_ads.accounts.domain.events import AdEntityDrifted
from safent_ads.accounts.domain.learning_state import LearningState
from safent_ads.accounts.domain.money import Money
from safent_ads.accounts.domain.platform_state_hash import PlatformStateHash
from safent_ads.shared.ids import BusinessId, EntityLevel, EntityRef, PlatformCode

_NOW = datetime(2026, 9, 9, tzinfo=UTC)
_HASH_A = PlatformStateHash.compute({"status": "ACTIVE"})
_HASH_B = PlatformStateHash.compute({"status": "PAUSED"})


def _campaign(**overrides: object) -> AdEntity:
    defaults: dict[str, object] = {
        "business_id": BusinessId.new(),
        "entity_ref": EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "1"),
        "parent_ref": EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "acc-1"),
        "name": "Campana Otoño",
        "status": AdEntityStatus.ACTIVE,
        "platform_state_hash": _HASH_A,
        "is_controllable": True,
    }
    defaults.update(overrides)
    return AdEntity(**defaults)  # type: ignore[arg-type]


def test_level_property_matches_entity_ref() -> None:
    assert _campaign().level == EntityLevel.CAMPAIGN


def test_account_level_is_rejected() -> None:
    with pytest.raises(InvalidEntityHierarchyError):
        _campaign(
            entity_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "acc-1"),
            parent_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.ACCOUNT, "root"),
        )


def test_wrong_parent_level_raises() -> None:
    with pytest.raises(InvalidEntityHierarchyError):
        _campaign(parent_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "wrong-level"))


def test_ad_set_requires_campaign_parent() -> None:
    ad_set = _campaign(
        entity_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.AD_SET, "2"),
        parent_ref=EntityRef(PlatformCode.GOOGLE, EntityLevel.CAMPAIGN, "1"),
    )

    assert ad_set.level == EntityLevel.AD_SET


def test_mark_drifted_changes_status_and_emits_event() -> None:
    entity = _campaign()

    entity.mark_drifted(_HASH_B, occurred_at=_NOW)

    assert entity.status == AdEntityStatus.DRIFTED
    events = entity.pull_events()
    assert len(events) == 1
    drift_event = events[0]
    assert isinstance(drift_event, AdEntityDrifted)
    assert drift_event.expected_hash == _HASH_A
    assert drift_event.observed_hash == _HASH_B


def test_mark_drifted_is_idempotent() -> None:
    entity = _campaign()

    entity.mark_drifted(_HASH_B, occurred_at=_NOW)
    entity.pull_events()
    entity.mark_drifted(_HASH_B, occurred_at=_NOW)

    assert entity.pull_events() == []


def test_change_budget_on_removed_entity_raises() -> None:
    entity = _campaign(status=AdEntityStatus.REMOVED)

    with pytest.raises(InvalidStateTransitionError):
        entity.change_budget(Budget(Money(1000, "EUR"), BudgetKind.DAILY), occurred_at=_NOW)


def test_mark_removed_from_platform_flips_status_to_removed() -> None:
    # M1 (repaso 0.2.23): `SyncAccountInventory` ya no encuentra este
    # `entity_ref` en el inventario remoto fresco.
    entity = _campaign(status=AdEntityStatus.ACTIVE)

    entity.mark_removed_from_platform()

    assert entity.status is AdEntityStatus.REMOVED
    assert entity.pull_events() == []


def test_confirm_activation_flips_a_paused_campaign_to_active() -> None:
    # Item 2 (repaso 0.2.23): `ACTIVATE_CAMPAIGN` sube a ACTIVE la campaña
    # que su propio `CREATE_CAMPAIGN` registro PAUSED, y refresca el hash
    # al del recibo confirmado -- sin evento propio, a diferencia de
    # `mark_drifted`.
    entity = _campaign(status=AdEntityStatus.PAUSED, platform_state_hash=_HASH_B)

    entity.confirm_activation(_HASH_A)

    assert entity.status is AdEntityStatus.ACTIVE
    assert entity.platform_state_hash == _HASH_A
    assert entity.pull_events() == []


def test_change_budget_emits_event_with_previous_value() -> None:
    entity = _campaign(budget=Budget(Money(500, "EUR"), BudgetKind.DAILY))

    entity.change_budget(Budget(Money(1000, "EUR"), BudgetKind.DAILY), occurred_at=_NOW)

    events = entity.pull_events()
    assert events[0].previous_budget == Budget(Money(500, "EUR"), BudgetKind.DAILY)
    assert entity.budget == Budget(Money(1000, "EUR"), BudgetKind.DAILY)


def test_learning_cycle() -> None:
    entity = _campaign()

    entity.enter_learning(occurred_at=_NOW)
    assert entity.learning_state == LearningState.LEARNING

    entity.complete_learning(occurred_at=_NOW)
    assert entity.learning_state == LearningState.LEARNED

    events = entity.pull_events()
    assert len(events) == 2


def test_complete_learning_without_entering_raises() -> None:
    entity = _campaign()

    with pytest.raises(InvalidStateTransitionError):
        entity.complete_learning(occurred_at=_NOW)


def test_refresh_from_platform_updates_plain_fields_without_events() -> None:
    entity = _campaign()

    entity.refresh_from_platform(
        name="Campana renombrada",
        status=AdEntityStatus.PAUSED,
        is_controllable=False,
        learning_state=LearningState.NOT_APPLICABLE,
        budget=None,
        bid_target=None,
        shared_budget_ref=None,
        new_hash=_HASH_B,
        occurred_at=_NOW,
    )

    assert entity.name == "Campana renombrada"
    assert entity.status == AdEntityStatus.PAUSED
    assert entity.is_controllable is False
    assert entity.platform_state_hash == _HASH_B
    assert entity.pull_events() == []


def test_refresh_from_platform_emits_budget_changed_when_budget_differs() -> None:
    entity = _campaign(budget=Budget(Money(500, "EUR"), BudgetKind.DAILY))

    entity.refresh_from_platform(
        name=entity.name,
        status=entity.status,
        is_controllable=entity.is_controllable,
        learning_state=entity.learning_state,
        budget=Budget(Money(900, "EUR"), BudgetKind.DAILY),
        bid_target=None,
        shared_budget_ref=None,
        new_hash=_HASH_B,
        occurred_at=_NOW,
    )

    events = entity.pull_events()
    assert len(events) == 1
    assert events[0].new_budget == Budget(Money(900, "EUR"), BudgetKind.DAILY)


def test_refresh_from_platform_enters_learning_and_emits_event() -> None:
    entity = _campaign()

    entity.refresh_from_platform(
        name=entity.name,
        status=entity.status,
        is_controllable=entity.is_controllable,
        learning_state=LearningState.LEARNING,
        budget=None,
        bid_target=None,
        shared_budget_ref=None,
        new_hash=_HASH_B,
        occurred_at=_NOW,
    )

    assert entity.learning_state == LearningState.LEARNING
    events = entity.pull_events()
    assert len(events) == 1
