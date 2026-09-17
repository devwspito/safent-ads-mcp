"""`PublishAutoReceipt` (contracts/telegram.md §Recibo de accion autonoma):
`[↩️ Deshacer]` ligado a un nonce cuyo TTL es la propia `undo_deadline`."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.notifications.application.dto import AutoReceiptEvent
from safent_ads.notifications.application.publish_auto_receipt import PublishAutoReceipt
from safent_ads.notifications.domain.callback import CallbackAction
from safent_ads.notifications.testing.fakes import (
    FakeMessenger,
    FakeNotificationOutbox,
    FakeTelegramCallbackStore,
)
from safent_ads.shared.ids import BusinessId, UuidIdGenerator

_DECIDED_AT = datetime(2026, 9, 9, 14, 12, tzinfo=UTC)
_UNDO_DEADLINE = datetime(2026, 9, 9, 14, 42, tzinfo=UTC)


def _event() -> AutoReceiptEvent:
    return AutoReceiptEvent(
        business_name="Negocio Ejemplo",
        entity_name="Búsqueda Marca",
        platform_label="Meta",
        parameter_label="Presupuesto",
        before_label="120 €/día",
        after_label="84 €/día",
        change_note="−30 %",
        cause_text="ROAS bajo objetivo en 3D y 7D",
        rule_id="M05",
        guardrail_note="suelo 60 €/día",
        change_ordinal=1,
        change_max=2,
        decided_at=_DECIDED_AT,
        undo_deadline=_UNDO_DEADLINE,
        proposal_id="9f3a1c07-1234-4321-8888-abcdefabcdef",
        diff_hash="a" * 64,
    )


async def test_sends_receipt_with_a_single_undo_button() -> None:
    messenger = FakeMessenger()
    store = FakeTelegramCallbackStore()
    use_case = PublishAutoReceipt(
        outbox=FakeNotificationOutbox(),
        messenger=messenger,
        callback_store=store,
        id_generator=UuidIdGenerator(),
    )

    await use_case.execute(business_id=BusinessId.new(), owner_chat_ids=[111], event=_event())

    keyboard = messenger.sent[0].reply_markup
    assert keyboard is not None
    assert sum(len(row) for row in keyboard) == 1
    (row,) = store._rows.values()
    assert row.action is CallbackAction.UNDO
    assert row.expires_at == _UNDO_DEADLINE


async def test_delivery_failure_creates_no_undo_button_nonce() -> None:
    messenger = FakeMessenger()
    messenger.fail_next = True
    store = FakeTelegramCallbackStore()
    use_case = PublishAutoReceipt(
        outbox=FakeNotificationOutbox(),
        messenger=messenger,
        callback_store=store,
        id_generator=UuidIdGenerator(),
    )

    await use_case.execute(business_id=BusinessId.new(), owner_chat_ids=[111], event=_event())

    assert store._rows == {}
