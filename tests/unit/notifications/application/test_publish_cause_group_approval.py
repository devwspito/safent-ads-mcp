"""`PublishCauseGroupApproval` (contracts/telegram.md §Lote por causa): dos
botones ligados al proposal_id ancla, sin nonces huerfanos si el envio
falla."""

from __future__ import annotations

from datetime import UTC, datetime

from safent_ads.notifications.application.dto import (
    ApprovalRequestView,
    CauseGroupApprovalView,
    SignalKind,
)
from safent_ads.notifications.application.publish_cause_group_approval import (
    PublishCauseGroupApproval,
)
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


def _item(entity_name: str, proposal_id: str) -> ApprovalRequestView:
    return ApprovalRequestView(
        proposal_id=proposal_id,
        diff_hash="a" * 64,
        entity_name=entity_name,
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


def _group() -> CauseGroupApprovalView:
    items = (
        _item("Secundaria Madrid", "9f3a1c07-1234-4321-8888-abcdefabcdef"),
        _item("Primaria Valencia", "aaaaaaaa-1234-4321-8888-abcdefabcdef"),
    )
    return CauseGroupApprovalView(
        cause_text="limitadas por presupuesto con CPL bajo objetivo",
        total_impact_label="+1.940 €/mes",
        anchor_proposal_id=items[0].proposal_id,
        anchor_diff_hash=items[0].diff_hash,
        items=items,
    )


async def test_sends_a_two_button_keyboard_bound_to_the_anchor() -> None:
    messenger = FakeMessenger()
    store = FakeTelegramCallbackStore()
    use_case = PublishCauseGroupApproval(
        outbox=FakeNotificationOutbox(),
        messenger=messenger,
        callback_store=store,
        id_generator=UuidIdGenerator(),
        clock=_FixedClock(),
    )

    await use_case.execute(business_id=BusinessId.new(), owner_chat_ids=[111], group=_group())

    keyboard = messenger.sent[0].reply_markup
    assert keyboard is not None
    assert sum(len(row) for row in keyboard) == 2
    assert len(store._rows) == 2
    for row in store._rows.values():
        assert row.proposal_id == "9f3a1c07-1234-4321-8888-abcdefabcdef"


async def test_delivery_failure_leaves_no_nonces() -> None:
    messenger = FakeMessenger()
    messenger.fail_next = True
    store = FakeTelegramCallbackStore()
    use_case = PublishCauseGroupApproval(
        outbox=FakeNotificationOutbox(),
        messenger=messenger,
        callback_store=store,
        id_generator=UuidIdGenerator(),
        clock=_FixedClock(),
    )

    await use_case.execute(business_id=BusinessId.new(), owner_chat_ids=[111], group=_group())

    assert store._rows == {}
