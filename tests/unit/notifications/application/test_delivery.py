"""`deliver_and_record`/`fan_out_to_owner_chats`: un fallo de entrega se
registra en logs, pero nunca con el `chat_id` en claro (repo rule: "no
PII/chat ids in logs (mask)")."""

from __future__ import annotations

import structlog.testing

from safent_ads.notifications.application.delivery import fan_out_to_owner_chats
from safent_ads.notifications.domain.value_objects import NotificationKind
from safent_ads.notifications.testing.fakes import FakeMessenger, FakeNotificationOutbox
from safent_ads.shared.ids import BusinessId, UuidIdGenerator

_CHAT_ID = 123456789


async def test_delivery_failure_logs_only_the_masked_chat_id() -> None:
    messenger = FakeMessenger()
    messenger.fail_next = True
    outbox = FakeNotificationOutbox()

    with structlog.testing.capture_logs() as captured:
        await fan_out_to_owner_chats(
            business_id=BusinessId.new(),
            kind=NotificationKind.APPROVAL_REQUEST,
            body="texto",
            base_dedupe_key="dedupe",
            owner_chat_ids=[_CHAT_ID],
            outbox=outbox,
            messenger=messenger,
            id_generator=UuidIdGenerator(),
        )

    failure_events = [
        event for event in captured if event["event"] == "notification_delivery_failed"
    ]
    assert len(failure_events) == 1
    logged_values = str(failure_events[0].values())
    assert str(_CHAT_ID) not in logged_values
    assert failure_events[0]["chat_id_masked"] == "***6789"
