"""`PublishApprovalRequest` (contracts/telegram.md §Solicitud de
aprobacion): manda la tarjeta con teclado y crea un nonce por boton SOLO
tras confirmar la entrega -- nunca antes, para no dejar nonces huerfanos
de un mensaje que nunca llego."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog.testing

from safent_ads.notifications.application.dto import ApprovalRequestView, SignalKind
from safent_ads.notifications.application.publish_approval_request import PublishApprovalRequest
from safent_ads.notifications.domain.callback import CallbackAction
from safent_ads.notifications.testing.fakes import (
    FakeMessenger,
    FakeNotificationOutbox,
    FakeTelegramCallbackStore,
)
from safent_ads.shared.ids import BusinessId, UuidIdGenerator

_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)


class _FixedClock:
    def now(self) -> datetime:
        return _NOW


def _view() -> ApprovalRequestView:
    return ApprovalRequestView(
        proposal_id="9f3a1c07-1234-4321-8888-abcdefabcdef",
        diff_hash="a" * 64,
        entity_name="Secundaria Madrid",
        platform_label="Google",
        kind=SignalKind.BUY,
        parameter_label="Presupuesto",
        before_label="90 €/día",
        after_label="117 €/día",
        change_note="+30 %",
        cause_text="limitada por presupuesto",
        window_label="7D",
        impact_label="+810 €/mes",
        rule_id="G01",
        expires_at=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
        is_spend_increase=True,
    )


def _use_case(
    messenger: FakeMessenger, store: FakeTelegramCallbackStore
) -> PublishApprovalRequest:
    return PublishApprovalRequest(
        outbox=FakeNotificationOutbox(),
        messenger=messenger,
        callback_store=store,
        id_generator=UuidIdGenerator(),
        clock=_FixedClock(),
    )


async def test_sends_the_card_with_a_four_button_keyboard() -> None:
    messenger = FakeMessenger()
    use_case = _use_case(messenger, FakeTelegramCallbackStore())

    sent = await use_case.execute(business_id=BusinessId.new(), owner_chat_ids=[111], view=_view())

    assert len(sent) == 1
    assert len(messenger.sent) == 1
    keyboard = messenger.sent[0].reply_markup
    assert keyboard is not None
    assert sum(len(row) for row in keyboard) == 4


async def test_registers_one_nonce_per_button_bound_to_the_real_message_id() -> None:
    messenger = FakeMessenger()
    store = FakeTelegramCallbackStore()
    use_case = _use_case(messenger, store)

    await use_case.execute(business_id=BusinessId.new(), owner_chat_ids=[111], view=_view())

    keyboard = messenger.sent[0].reply_markup
    assert keyboard is not None
    nonces = [button.callback_data.split(":")[2] for row in keyboard for button in row]
    assert len(nonces) == 4
    for nonce in nonces:
        record = await store.peek(nonce=nonce, chat_id=111, message_id=1, now=_NOW)
        assert record is not None
        assert record.proposal_id == "9f3a1c07-1234-4321-8888-abcdefabcdef"
        assert record.diff_hash == "a" * 64


async def test_delivery_failure_creates_no_nonces() -> None:
    messenger = FakeMessenger()
    messenger.fail_next = True
    store = FakeTelegramCallbackStore()
    use_case = _use_case(messenger, store)

    sent = await use_case.execute(business_id=BusinessId.new(), owner_chat_ids=[111], view=_view())

    assert sent == [] or sent[0].delivery_state.value == "failed"
    assert store._rows == {}


async def test_delivery_failure_logs_only_the_masked_chat_id() -> None:
    """Repo rule: "no PII/chat ids in logs (mask)" -- `_send_and_register`
    debe registrar `chat_id_masked`, nunca el `chat_id` en claro."""
    messenger = FakeMessenger()
    messenger.fail_next = True
    store = FakeTelegramCallbackStore()
    use_case = _use_case(messenger, store)
    chat_id = 987654321

    with structlog.testing.capture_logs() as captured:
        await use_case.execute(business_id=BusinessId.new(), owner_chat_ids=[chat_id], view=_view())

    failure_events = [
        event for event in captured if event["event"] == "approval_request_delivery_failed"
    ]
    assert len(failure_events) == 1
    assert str(chat_id) not in str(failure_events[0].values())
    assert failure_events[0]["chat_id_masked"] == "***4321"


async def test_second_delivery_attempt_is_deduplicated() -> None:
    messenger = FakeMessenger()
    outbox = FakeNotificationOutbox()
    use_case = PublishApprovalRequest(
        outbox=outbox,
        messenger=messenger,
        callback_store=FakeTelegramCallbackStore(),
        id_generator=UuidIdGenerator(),
        clock=_FixedClock(),
    )
    business_id = BusinessId.new()

    first = await use_case.execute(business_id=business_id, owner_chat_ids=[111], view=_view())
    second = await use_case.execute(business_id=business_id, owner_chat_ids=[111], view=_view())

    assert len(first) == 1
    assert second == []
    assert len(messenger.sent) == 1


async def test_detail_nonce_has_the_detail_action() -> None:
    messenger = FakeMessenger()
    store = FakeTelegramCallbackStore()
    use_case = _use_case(messenger, store)

    await use_case.execute(business_id=BusinessId.new(), owner_chat_ids=[111], view=_view())

    detail_rows = [row for row in store._rows.values() if row.action is CallbackAction.DETAIL]
    assert len(detail_rows) == 1
