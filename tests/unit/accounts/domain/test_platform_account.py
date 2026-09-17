"""Maquina de estados de `PlatformAccount` (data-model.md:
"ACTIVE -> THROTTLED -> ACTIVE, ACTIVE -> SUSPENDED (terminal hasta
reconciliacion manual), ACTIVE -> READ_ONLY (plataforma caida)")."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from safent_ads.accounts.domain.errors import InvalidStateTransitionError
from safent_ads.accounts.domain.events import PlatformAccountSuspended, PlatformAccountThrottled
from safent_ads.accounts.domain.platform_account import (
    ApiTier,
    PlatformAccount,
    PlatformAccountStatus,
)
from safent_ads.accounts.domain.refs import AccountRef, CredentialRefId
from safent_ads.shared.ids import BusinessId, PlatformCode

_NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _account(**overrides: object) -> PlatformAccount:
    defaults: dict[str, object] = {
        "business_id": BusinessId.new(),
        "account_ref": AccountRef(PlatformCode.GOOGLE, "123-456-7890"),
        "currency": "EUR",
        "timezone": "Europe/Madrid",
        "api_tier": ApiTier.GOOGLE_EXPLORER,
        "credential_ref_id": CredentialRefId(uuid.uuid4()),
    }
    defaults.update(overrides)
    return PlatformAccount(**defaults)  # type: ignore[arg-type]


def test_starts_active() -> None:
    assert _account().status == PlatformAccountStatus.ACTIVE


def test_active_to_throttled_and_back() -> None:
    account = _account()

    account.mark_throttled(occurred_at=_NOW)
    assert account.status == PlatformAccountStatus.THROTTLED

    account.mark_active()
    assert account.status == PlatformAccountStatus.ACTIVE


def test_throttled_emits_event() -> None:
    account = _account()

    account.mark_throttled(occurred_at=_NOW)

    events = account.pull_events()
    assert len(events) == 1
    assert isinstance(events[0], PlatformAccountThrottled)


def test_pull_events_drains_the_queue() -> None:
    account = _account()
    account.mark_throttled(occurred_at=_NOW)

    first_pull = account.pull_events()
    second_pull = account.pull_events()

    assert len(first_pull) == 1
    assert second_pull == []


def test_suspend_is_terminal_until_reconcile() -> None:
    account = _account()

    account.suspend(occurred_at=_NOW, reason="fraude sospechoso")
    assert account.status == PlatformAccountStatus.SUSPENDED

    with pytest.raises(InvalidStateTransitionError):
        account.mark_active()

    account.reconcile()
    assert account.status == PlatformAccountStatus.ACTIVE


def test_suspend_emits_event_with_reason() -> None:
    account = _account()

    account.suspend(occurred_at=_NOW, reason="fraude sospechoso")

    events = account.pull_events()
    assert isinstance(events[0], PlatformAccountSuspended)
    assert events[0].reason == "fraude sospechoso"


def test_reconcile_from_non_suspended_raises() -> None:
    account = _account()

    with pytest.raises(InvalidStateTransitionError):
        account.reconcile()


def test_read_only_recovers_to_active() -> None:
    account = _account()

    account.mark_read_only()
    assert account.status == PlatformAccountStatus.READ_ONLY

    account.mark_active()
    assert account.status == PlatformAccountStatus.ACTIVE


def test_suspended_account_cannot_sync() -> None:
    account = _account()
    account.suspend(occurred_at=_NOW, reason="fraude sospechoso")

    with pytest.raises(InvalidStateTransitionError):
        account.record_synced(at=_NOW)


def test_record_synced_updates_timestamp() -> None:
    account = _account()

    account.record_synced(at=_NOW)

    assert account.last_synced_at == _NOW
