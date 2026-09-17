"""`DeliverTelegramTestMessages` (`ads-worker` drena `telegram_test_messages`,
FR-25): entrega con el `MessengerPort` real, marca `SENT`/`FAILED`, un
fallo de entrega nunca deja la tarea en el limbo."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from safent_ads.notifications.application.telegram_pairing import DeliverTelegramTestMessages
from safent_ads.notifications.testing.fakes import FakeMessenger, FakeTestMessageOutbox

_CHAT_ID = 111222333
_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)


async def test_delivers_every_pending_message() -> None:
    outbox = FakeTestMessageOutbox()
    messenger = FakeMessenger()
    await outbox.enqueue(
        notification_id=uuid.uuid4(), owner_id=uuid.uuid4(), chat_id=_CHAT_ID, body="hola", at=_NOW
    )
    deliver = DeliverTelegramTestMessages(outbox=outbox, messenger=messenger)

    delivered = await deliver.execute()

    assert delivered == 1
    assert len(messenger.sent) == 1
    assert messenger.sent[0].chat_id == _CHAT_ID


async def test_marks_delivered_messages_as_sent() -> None:
    outbox = FakeTestMessageOutbox()
    notification_id = uuid.uuid4()
    await outbox.enqueue(
        notification_id=notification_id,
        owner_id=uuid.uuid4(),
        chat_id=_CHAT_ID,
        body="hola",
        at=_NOW,
    )
    deliver = DeliverTelegramTestMessages(outbox=outbox, messenger=FakeMessenger())

    await deliver.execute()

    assert outbox.sent == [notification_id]
    assert await outbox.claim_pending(limit=10) == []


async def test_delivery_failure_marks_the_message_failed_not_stuck_pending() -> None:
    outbox = FakeTestMessageOutbox()
    notification_id = uuid.uuid4()
    await outbox.enqueue(
        notification_id=notification_id,
        owner_id=uuid.uuid4(),
        chat_id=_CHAT_ID,
        body="hola",
        at=_NOW,
    )
    messenger = FakeMessenger()
    messenger.fail_next = True
    deliver = DeliverTelegramTestMessages(outbox=outbox, messenger=messenger)

    delivered = await deliver.execute()

    assert delivered == 0
    assert outbox.failed == [notification_id]
    assert await outbox.claim_pending(limit=10) == []


async def test_no_pending_messages_delivers_nothing() -> None:
    deliver = DeliverTelegramTestMessages(
        outbox=FakeTestMessageOutbox(), messenger=FakeMessenger()
    )

    delivered = await deliver.execute()

    assert delivered == 0
