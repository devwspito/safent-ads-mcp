"""`ListPendingProposals` (`/pendientes`): reenvia tarjetas de aprobacion
nivel 1 con nonces nuevos, mismo emparejamiento obligatorio (sin decision
de por medio) que `ReportBusinessStatus`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.notifications.application.dto import SignalKind
from safent_ads.notifications.application.list_pending_proposals import ListPendingProposals
from safent_ads.notifications.application.ports import LiveProposalView
from safent_ads.notifications.testing.fakes import (
    FakeApprovalGateway,
    FakeMessenger,
    FakePendingProposalIds,
    FakeTelegramCallbackStore,
    FakeTelegramPairingGuard,
)
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
_CHAT_ID = 111222333


def _view(proposal_id: str, **overrides: object) -> LiveProposalView:
    defaults: dict[str, object] = {
        "proposal_id": proposal_id,
        "diff_hash": "a" * 64,
        "state_label": "pendiente",
        "is_pending": True,
        "entity_name": "Secundaria Madrid",
        "platform_label": "Google",
        "kind": SignalKind.BUY,
        "parameter_label": "Presupuesto",
        "before_label": "90 €/día",
        "after_label": "117 €/día",
        "change_note": None,
        "cause_text": "limitada por presupuesto",
        "rule_id": "G01",
        "window_label": "7D",
        "impact_label": "+810 €/mes",
        "expires_at": _NOW + timedelta(days=3),
        "is_spend_increase": True,
    }
    defaults.update(overrides)
    return LiveProposalView(**defaults)  # type: ignore[arg-type]


def _build(
    *,
    ids: tuple[str, ...] = (),
    gateway: FakeApprovalGateway | None = None,
    pairing_guard: FakeTelegramPairingGuard | None = None,
) -> tuple[ListPendingProposals, FakeMessenger, FakeTelegramCallbackStore]:
    messenger = FakeMessenger()
    callback_store = FakeTelegramCallbackStore()
    use_case = ListPendingProposals(
        pending_ids=FakePendingProposalIds(ids),
        gateway=gateway or FakeApprovalGateway(),
        callback_store=callback_store,
        pairing_guard=pairing_guard or FakeTelegramPairingGuard(),
        messenger=messenger,
        clock=FixedClock(_NOW),
    )
    return use_case, messenger, callback_store


async def test_authorized_and_paired_sends_one_card_per_pending_proposal() -> None:
    gateway = FakeApprovalGateway()
    gateway.live["p-1"] = _view("p-1", entity_name="Campaña Uno")
    gateway.live["p-2"] = _view("p-2", entity_name="Campaña Dos")
    use_case, messenger, callback_store = _build(ids=("p-1", "p-2"), gateway=gateway)

    sent = await use_case.execute(chat_id=_CHAT_ID)

    assert sent == 2
    assert len(messenger.sent) == 2
    assert "Campaña Uno" in messenger.sent[0].text
    assert "Campaña Dos" in messenger.sent[1].text
    assert messenger.sent[0].reply_markup is not None
    first_approve_nonce = messenger.sent[0].reply_markup[0][0].callback_data.split(":")[2]
    second_approve_nonce = messenger.sent[1].reply_markup[0][0].callback_data.split(":")[2]  # type: ignore[index]
    assert first_approve_nonce != second_approve_nonce  # nonces nuevos, nunca reutilizados
    assert await callback_store.find_by_nonce(first_approve_nonce) is not None


async def test_no_pending_proposals_sends_a_single_friendly_message() -> None:
    use_case, messenger, _store = _build(ids=())

    sent = await use_case.execute(chat_id=_CHAT_ID)

    assert sent == 0
    assert len(messenger.sent) == 1
    assert "No hay propuestas pendientes" in messenger.sent[0].text


async def test_allow_listed_but_unpaired_replies_sin_emparejar_without_a_decision() -> None:
    guard = FakeTelegramPairingGuard(paired=False)
    use_case, messenger, _store = _build(ids=("p-1",), pairing_guard=guard)

    sent = await use_case.execute(chat_id=_CHAT_ID)

    assert sent == 0
    assert len(messenger.sent) == 1
    assert messenger.sent[0].text == "Sin emparejar"
    assert guard.denied_chat_ids == []
