"""`PublishDigest` (T042): vacia la cola con `disable_notification=true`
(contracts/telegram.md §Digest)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from safent_ads.notifications.application.publish_digest import PublishDigest
from safent_ads.notifications.testing.fakes import (
    FakeMessenger,
    FakeNotificationOutbox,
    FakePendingDigest,
)
from safent_ads.shared.ids import BusinessId, UuidIdGenerator
from tests.unit.notifications.application.conftest import make_signal

_MADRID = ZoneInfo("Europe/Madrid")


async def test_digest_flushes_pending_with_disable_notification(
    business_id: BusinessId,
) -> None:
    pending_digest = FakePendingDigest()
    outbox = FakeNotificationOutbox()
    messenger = FakeMessenger()
    await pending_digest.enqueue(
        business_id,
        [make_signal(name="Nocturna")],
        queued_at=datetime(2026, 9, 9, 23, 30, tzinfo=_MADRID),
    )
    use_case = PublishDigest(
        pending_digest=pending_digest,
        outbox=outbox,
        messenger=messenger,
        id_generator=UuidIdGenerator(),
    )

    sent = await use_case.execute(
        business_id=business_id,
        business_name="Negocio Ejemplo",
        owner_chat_ids=[111],
        now=datetime(2026, 9, 10, 8, 0, tzinfo=_MADRID),
    )

    assert len(sent) == 1
    assert sent[0].kind.value == "digest"
    assert messenger.sent[0].disable_notification is True
    assert "Nocturna" in sent[0].body


async def test_digest_with_nothing_pending_sends_nothing(business_id: BusinessId) -> None:
    use_case = PublishDigest(
        pending_digest=FakePendingDigest(),
        outbox=FakeNotificationOutbox(),
        messenger=FakeMessenger(),
        id_generator=UuidIdGenerator(),
    )

    sent = await use_case.execute(
        business_id=business_id,
        business_name="Negocio Ejemplo",
        owner_chat_ids=[111],
        now=datetime(2026, 9, 10, 8, 0, tzinfo=_MADRID),
    )

    assert sent == []
