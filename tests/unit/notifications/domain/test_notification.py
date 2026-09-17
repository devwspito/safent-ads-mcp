"""Agregado `Notification` y `DedupeKey` (T040, NFR-6)."""

from __future__ import annotations

import uuid

import pytest

from safent_ads.notifications.domain.dedupe_guard import DuplicateSendGuard
from safent_ads.notifications.domain.errors import (
    EmptyDedupeKeyError,
    EmptyNotificationBodyError,
    NotificationAlreadySentError,
    NotificationTerminalStateError,
)
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.value_objects import (
    Channel,
    DedupeKey,
    DeliveryState,
    NotificationKind,
    Severity,
)
from safent_ads.shared.ids import BusinessId


def _notification(dedupe_key: str = "ticker:biz1:2026-09-09T14:00") -> Notification:
    return Notification(
        notification_id=uuid.uuid4(),
        business_id=BusinessId.new(),
        channel=Channel.TELEGRAM,
        severity=Severity.INFO,
        kind=NotificationKind.TICKER,
        dedupe_key=DedupeKey(dedupe_key),
        body="📊 Negocio Ejemplo · 14:00",
    )


def test_new_notification_starts_pending() -> None:
    notification = _notification()

    assert notification.delivery_state is DeliveryState.PENDING
    assert notification.delivery_attempts == 0


def test_mark_sent_transitions_to_terminal_state() -> None:
    notification = _notification()

    notification.mark_sent()

    assert notification.delivery_state is DeliveryState.SENT
    assert notification.is_terminal


def test_mark_sent_twice_raises() -> None:
    notification = _notification()
    notification.mark_sent()

    with pytest.raises(NotificationAlreadySentError):
        notification.mark_sent()


def test_mark_failed_records_attempts_and_state() -> None:
    notification = _notification()

    notification.mark_failed(attempts=3)

    assert notification.delivery_state is DeliveryState.FAILED
    assert notification.delivery_attempts == 3


def test_mark_failed_after_sent_raises() -> None:
    notification = _notification()
    notification.mark_sent()

    with pytest.raises(NotificationTerminalStateError):
        notification.mark_failed(attempts=1)


def test_empty_body_rejected() -> None:
    with pytest.raises(EmptyNotificationBodyError):
        Notification(
            notification_id=uuid.uuid4(),
            business_id=BusinessId.new(),
            channel=Channel.TELEGRAM,
            severity=Severity.INFO,
            kind=NotificationKind.TICKER,
            dedupe_key=DedupeKey("k"),
            body="   ",
        )


def test_empty_dedupe_key_rejected() -> None:
    with pytest.raises(EmptyDedupeKeyError):
        DedupeKey("   ")


def test_equal_dedupe_key_value_compares_equal() -> None:
    """Dos reconstrucciones de "la misma" notificacion logica (mismo
    negocio+tipo+contenido) deben producir un `DedupeKey` igual: es la base
    de que el guardian detecte el reintento (NFR-6)."""
    assert DedupeKey("ticker:biz1:2026-09-09T14:00") == DedupeKey(
        "ticker:biz1:2026-09-09T14:00"
    )


def test_dedupe_key_prevents_double_send() -> None:
    guard = DuplicateSendGuard()
    key = DedupeKey("ticker:biz1:2026-09-09T14:00")

    first_attempt_should_send = guard.register(key)
    retry_should_send = guard.register(key)

    assert first_attempt_should_send is True
    assert retry_should_send is False
    assert guard.already_sent(key) is True


def test_dedupe_key_allows_distinct_notifications() -> None:
    guard = DuplicateSendGuard()

    assert guard.register(DedupeKey("ticker:biz1:14:00")) is True
    assert guard.register(DedupeKey("ticker:biz1:15:00")) is True
