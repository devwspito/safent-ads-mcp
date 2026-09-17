"""`PublishCritical` (T042): interrumpe siempre; no se acumula en digest
(contracts/telegram.md §Critico)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from safent_ads.notifications.application.dto import CriticalEvent
from safent_ads.notifications.application.publish_critical import PublishCritical
from safent_ads.notifications.testing.fakes import FakeMessenger, FakeNotificationOutbox
from safent_ads.shared.ids import BusinessId, UuidIdGenerator

_MADRID = ZoneInfo("Europe/Madrid")


def _event(occurred_at: datetime) -> CriticalEvent:
    return CriticalEvent(
        business_name="Negocio Ejemplo",
        title="Cuenta Meta act_1029… SUSPENDIDA por la plataforma.",
        detail="Ninguna acción es posible. Revisa en Business Manager.",
        note="sin acción automática",
        occurred_at=occurred_at,
    )


async def test_critical_sends_outside_active_hours() -> None:
    business_id = BusinessId.new()
    messenger = FakeMessenger()
    use_case = PublishCritical(
        outbox=FakeNotificationOutbox(), messenger=messenger, id_generator=UuidIdGenerator()
    )

    sent = await use_case.execute(
        business_id=business_id,
        owner_chat_ids=[111],
        event=_event(datetime(2026, 9, 9, 3, 0, tzinfo=_MADRID)),
    )

    assert len(sent) == 1
    # la severidad de canal la fija el llamador; kind=CRITICAL es lo que manda
    assert sent[0].severity.value == "info"
    assert sent[0].kind.value == "critical"
    assert messenger.sent[0].disable_notification is False
    assert "🚨 CRÍTICO · Negocio Ejemplo" in sent[0].body


async def test_critical_repeated_within_the_same_hour_does_not_duplicate() -> None:
    business_id = BusinessId.new()
    outbox = FakeNotificationOutbox()
    messenger = FakeMessenger()
    use_case = PublishCritical(
        outbox=outbox, messenger=messenger, id_generator=UuidIdGenerator()
    )
    event = _event(datetime(2026, 9, 9, 14, 7, tzinfo=_MADRID))

    first = await use_case.execute(business_id=business_id, owner_chat_ids=[111], event=event)
    second = await use_case.execute(
        business_id=business_id,
        owner_chat_ids=[111],
        event=_event(datetime(2026, 9, 9, 14, 40, tzinfo=_MADRID)),
    )

    assert len(first) == 1
    assert second == []


async def test_different_critical_titles_both_notify() -> None:
    business_id = BusinessId.new()
    messenger = FakeMessenger()
    use_case = PublishCritical(
        outbox=FakeNotificationOutbox(), messenger=messenger, id_generator=UuidIdGenerator()
    )
    now = datetime(2026, 9, 9, 14, 7, tzinfo=_MADRID)

    await use_case.execute(business_id=business_id, owner_chat_ids=[111], event=_event(now))
    second_event = CriticalEvent(
        business_name="Negocio Ejemplo",
        title="Freno de emergencia activado.",
        detail="Ninguna accion automatica hasta desactivarlo.",
        note="sin accion automatica",
        occurred_at=now,
    )
    second = await use_case.execute(
        business_id=business_id, owner_chat_ids=[111], event=second_event
    )

    assert len(second) == 1
    assert len(messenger.sent) == 2
