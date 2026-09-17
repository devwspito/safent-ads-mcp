"""`PublishTicker` (T042): orden por dinero en juego, tope de 12 lineas +
pie, y enrutado dentro/fuera del horario activo (`test_ticker_ordering_and_digest_window`,
tarea explicita de T042)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from safent_ads.notifications.application.publish_ticker import PublishTicker
from safent_ads.notifications.domain.value_objects import ActiveHoursWindow
from safent_ads.notifications.testing.fakes import (
    FakeMessenger,
    FakeNotificationOutbox,
    FakePendingDigest,
    FakeSignalsForTicker,
)
from safent_ads.shared.ids import BusinessId, UuidIdGenerator
from tests.unit.notifications.application.conftest import make_signal

_MADRID = ZoneInfo("Europe/Madrid")


def _publish_ticker(
    *,
    signals: FakeSignalsForTicker,
    outbox: FakeNotificationOutbox,
    pending_digest: FakePendingDigest,
    messenger: FakeMessenger,
    active_hours: ActiveHoursWindow,
) -> PublishTicker:
    return PublishTicker(
        signals=signals,
        outbox=outbox,
        pending_digest=pending_digest,
        messenger=messenger,
        id_generator=UuidIdGenerator(),
        active_hours=active_hours,
    )


async def test_ticker_ordering_and_digest_window(
    business_id: BusinessId, madrid_active_hours: ActiveHoursWindow
) -> None:
    fifteen_signals = [
        make_signal(name=f"Campaña {i}", amount=str(i)) for i in range(1, 16)
    ]
    signals = FakeSignalsForTicker({business_id: fifteen_signals})
    outbox = FakeNotificationOutbox()
    pending_digest = FakePendingDigest()
    messenger = FakeMessenger()
    use_case = _publish_ticker(
        signals=signals,
        outbox=outbox,
        pending_digest=pending_digest,
        messenger=messenger,
        active_hours=madrid_active_hours,
    )

    inside_active_hours = datetime(2026, 9, 9, 14, 0, tzinfo=_MADRID)
    sent = await use_case.execute(
        business_id=business_id,
        business_name="Negocio Ejemplo",
        owner_chat_ids=[111],
        now=inside_active_hours,
    )

    assert len(sent) == 1
    body = sent[0].body
    lines = body.splitlines()
    # cabecera + separador + 12 senales + pie con el resto
    assert lines[0] == "📊 Negocio Ejemplo · 14:00"
    signal_lines = lines[2:-1]  # tras cabecera + separador, antes del pie
    assert len(signal_lines) == 12
    assert "Campaña 15" in signal_lines[0]  # mayor dinero en juego primero
    assert "Campaña 4" in signal_lines[-1]  # el importe 12º mas alto es 4
    assert lines[-1] == "⏸ 3 señales menores · /pendientes"


async def test_outside_active_hours_queues_instead_of_sending(
    business_id: BusinessId, madrid_active_hours: ActiveHoursWindow
) -> None:
    signals = FakeSignalsForTicker({business_id: [make_signal()]})
    outbox = FakeNotificationOutbox()
    pending_digest = FakePendingDigest()
    messenger = FakeMessenger()
    use_case = _publish_ticker(
        signals=signals,
        outbox=outbox,
        pending_digest=pending_digest,
        messenger=messenger,
        active_hours=madrid_active_hours,
    )

    outside_active_hours = datetime(2026, 9, 9, 23, 30, tzinfo=_MADRID)
    sent = await use_case.execute(
        business_id=business_id,
        business_name="Negocio Ejemplo",
        owner_chat_ids=[111],
        now=outside_active_hours,
    )

    assert sent == []
    assert messenger.sent == []
    queued = await pending_digest.pop_due(business_id, at=outside_active_hours)
    assert len(queued) == 1


async def test_no_signals_sends_nothing(
    business_id: BusinessId, madrid_active_hours: ActiveHoursWindow
) -> None:
    signals = FakeSignalsForTicker({business_id: []})
    use_case = _publish_ticker(
        signals=signals,
        outbox=FakeNotificationOutbox(),
        pending_digest=FakePendingDigest(),
        messenger=FakeMessenger(),
        active_hours=madrid_active_hours,
    )

    sent = await use_case.execute(
        business_id=business_id,
        business_name="Negocio Ejemplo",
        owner_chat_ids=[111],
        now=datetime(2026, 9, 9, 14, 0, tzinfo=_MADRID),
    )

    assert sent == []


async def test_retry_with_same_minute_does_not_duplicate_message(
    business_id: BusinessId, madrid_active_hours: ActiveHoursWindow
) -> None:
    signals = FakeSignalsForTicker({business_id: [make_signal()]})
    outbox = FakeNotificationOutbox()
    messenger = FakeMessenger()
    use_case = _publish_ticker(
        signals=signals,
        outbox=outbox,
        pending_digest=FakePendingDigest(),
        messenger=messenger,
        active_hours=madrid_active_hours,
    )
    now = datetime(2026, 9, 9, 14, 0, tzinfo=_MADRID)

    first = await use_case.execute(
        business_id=business_id, business_name="Negocio Ejemplo", owner_chat_ids=[111], now=now
    )
    retry = await use_case.execute(
        business_id=business_id, business_name="Negocio Ejemplo", owner_chat_ids=[111], now=now
    )

    assert len(first) == 1
    assert retry == []
    assert len(messenger.sent) == 1


async def test_delivery_failure_marks_failed_and_does_not_raise(
    business_id: BusinessId, madrid_active_hours: ActiveHoursWindow
) -> None:
    signals = FakeSignalsForTicker({business_id: [make_signal()]})
    outbox = FakeNotificationOutbox()
    messenger = FakeMessenger()
    messenger.fail_next = True
    use_case = _publish_ticker(
        signals=signals,
        outbox=outbox,
        pending_digest=FakePendingDigest(),
        messenger=messenger,
        active_hours=madrid_active_hours,
    )

    sent = await use_case.execute(
        business_id=business_id,
        business_name="Negocio Ejemplo",
        owner_chat_ids=[111],
        now=datetime(2026, 9, 9, 14, 0, tzinfo=_MADRID),
    )

    assert len(sent) == 1
    assert sent[0].delivery_state.value == "failed"
    assert outbox.saved[0].delivery_state.value == "failed"


async def test_ticker_sends_once_per_owner_chat_id(
    business_id: BusinessId, madrid_active_hours: ActiveHoursWindow
) -> None:
    signals = FakeSignalsForTicker({business_id: [make_signal()]})
    messenger = FakeMessenger()
    use_case = _publish_ticker(
        signals=signals,
        outbox=FakeNotificationOutbox(),
        pending_digest=FakePendingDigest(),
        messenger=messenger,
        active_hours=madrid_active_hours,
    )

    sent = await use_case.execute(
        business_id=business_id,
        business_name="Negocio Ejemplo",
        owner_chat_ids=[111, 222],
        now=datetime(2026, 9, 9, 14, 0, tzinfo=_MADRID) + timedelta(minutes=1),
    )

    assert len(sent) == 2
    assert {message.chat_id for message in messenger.sent} == {111, 222}
