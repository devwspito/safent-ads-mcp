"""`ResolveCallback` (contracts/telegram.md): nonce de un solo uso, TTL,
INV-1 (reemite con nonce nuevo si el diff cambio), segundo toque para
SUBIR con rotacion de nonce, lote por causa con fallo parcial explicito, y
el recibo de `[Deshacer]` dentro/fuera de la ventana de gracia."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from safent_ads.notifications.application.dto import (
    ApprovalDetailView,
    ApprovalEvidenceLine,
    SignalKind,
)
from safent_ads.notifications.application.ports import (
    LiveProposalView,
    UndoResult,
    UndoResultKind,
)
from safent_ads.notifications.application.resolve_callback import ResolveCallback
from safent_ads.notifications.domain.callback import CallbackAction, CallbackData, generate_nonce
from safent_ads.notifications.testing.fakes import (
    FakeApprovalGateway,
    FakeTelegramCallbackStore,
    FakeTelegramPairingGuard,
)
from safent_ads.shared.clock import FixedClock

_NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
_CHAT_ID = 111222333
_MESSAGE_ID = 42
_FROM_USER_ID = 111222333
_PROPOSAL_ID = "9f3a1c07-1234-4321-8888-abcdefabcdef"
_DIFF_HASH = "a" * 64


def _view(**overrides: object) -> LiveProposalView:
    defaults: dict[str, object] = {
        "proposal_id": _PROPOSAL_ID,
        "diff_hash": _DIFF_HASH,
        "state_label": "pendiente",
        "is_pending": True,
        "entity_name": "Secundaria Madrid",
        "platform_label": "Google",
        "kind": SignalKind.BUY,
        "parameter_label": "Presupuesto",
        "before_label": "90 €/día",
        "after_label": "117 €/día",
        "change_note": "+30 %",
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
    *, pairing_guard: FakeTelegramPairingGuard | None = None
) -> tuple[ResolveCallback, FakeTelegramCallbackStore, FakeApprovalGateway]:
    store = FakeTelegramCallbackStore()
    gateway = FakeApprovalGateway()
    resolver = ResolveCallback(
        store=store,
        gateway=gateway,
        pairing_guard=pairing_guard or FakeTelegramPairingGuard(),
        clock=FixedClock(_NOW),
    )
    return resolver, store, gateway


async def _seed_nonce(
    store: FakeTelegramCallbackStore,
    *,
    action: CallbackAction,
    proposal_id: str = _PROPOSAL_ID,
    diff_hash: str = _DIFF_HASH,
    ttl: timedelta = timedelta(hours=6),
) -> str:
    nonce = generate_nonce()
    await store.create(
        nonce=nonce,
        proposal_id=proposal_id,
        chat_id=_CHAT_ID,
        message_id=_MESSAGE_ID,
        diff_hash=diff_hash,
        action=action,
        expires_at=_NOW + ttl,
    )
    return nonce


def _callback_data(*, proposal_id: str, nonce: str, action: CallbackAction) -> str:
    return CallbackData.build(proposal_id=proposal_id, nonce=nonce, action=action).encode()


async def _tap(
    resolver: ResolveCallback,
    *,
    nonce: str,
    action: CallbackAction,
    proposal_id: str = _PROPOSAL_ID,
):
    return await resolver.execute(
        callback_data=_callback_data(proposal_id=proposal_id, nonce=nonce, action=action),
        chat_id=_CHAT_ID,
        from_user_id=_FROM_USER_ID,
        message_id=_MESSAGE_ID,
    )


class TestNonceLifecycle:
    async def test_malformed_callback_data_is_treated_as_expired(self) -> None:
        resolver, _store, _gateway = _build()

        outcome = await resolver.execute(
            callback_data="libre texto",
            chat_id=_CHAT_ID,
            from_user_id=_FROM_USER_ID,
            message_id=_MESSAGE_ID,
        )

        assert outcome.alert_text == "Caducada"

    async def test_unknown_nonce_answers_expired_without_context(self) -> None:
        resolver, _store, _gateway = _build()

        outcome = await _tap(resolver, nonce=generate_nonce(), action=CallbackAction.APPROVE)

        assert outcome.alert_text == "Caducada"
        assert outcome.edited_text is None

    async def test_expired_nonce_reedits_with_real_state(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view(state_label="aprobada")
        nonce = await _seed_nonce(store, action=CallbackAction.APPROVE, ttl=timedelta(hours=-1))

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE)

        assert outcome.alert_text == "Caducada"
        assert outcome.edited_text is not None
        assert "aprobada" in outcome.edited_text
        assert outcome.reply_markup == ()

    async def test_consumed_nonce_cannot_be_used_twice(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view(is_spend_increase=False)
        nonce = await _seed_nonce(store, action=CallbackAction.REJECT)

        first = await _tap(resolver, nonce=nonce, action=CallbackAction.REJECT)
        second = await _tap(resolver, nonce=nonce, action=CallbackAction.REJECT)

        assert first.alert_text != "Caducada"
        assert second.alert_text == "Caducada"
        assert gateway.rejected == [_PROPOSAL_ID]  # solo una vez


class TestDiffChangedInvariant:
    async def test_diff_hash_mismatch_reissues_a_fresh_card(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view(diff_hash="b" * 64)
        nonce = await _seed_nonce(store, action=CallbackAction.APPROVE, diff_hash=_DIFF_HASH)

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE)

        assert outcome.alert_text == "La propuesta cambió"
        assert outcome.show_alert is True
        assert outcome.reply_markup is not None
        assert len(outcome.reply_markup) == 2  # 2 filas de botones, como la tarjeta nivel 1
        assert gateway.approved == []  # nunca aprueba a ciegas sobre terminos distintos


class TestApproveSecondTapForSpendIncrease:
    async def test_first_tap_on_spend_increase_does_not_approve(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view(is_spend_increase=True)
        nonce = await _seed_nonce(store, action=CallbackAction.APPROVE)

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE)

        assert gateway.approved == []
        assert "Confirmar subida" in (outcome.edited_text or "")
        assert outcome.reply_markup is not None

    async def test_second_tap_confirm_approves(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view(is_spend_increase=True)
        approve_nonce = await _seed_nonce(store, action=CallbackAction.APPROVE)
        await _tap(resolver, nonce=approve_nonce, action=CallbackAction.APPROVE)
        confirm_nonce = _only_nonce_for(store, action=CallbackAction.CONFIRM)

        outcome = await _tap(resolver, nonce=confirm_nonce, action=CallbackAction.CONFIRM)

        assert gateway.approved == [_PROPOSAL_ID]
        assert "Aprobado" in (outcome.edited_text or "")

    async def test_nonce_rotates_between_first_and_second_tap(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view(is_spend_increase=True)
        approve_nonce = await _seed_nonce(store, action=CallbackAction.APPROVE)

        await _tap(resolver, nonce=approve_nonce, action=CallbackAction.APPROVE)
        confirm_nonce = _only_nonce_for(store, action=CallbackAction.CONFIRM)

        assert confirm_nonce != approve_nonce
        # el nonce del primer toque ya esta consumido: reenviarlo no confirma solo
        replay = await _tap(resolver, nonce=approve_nonce, action=CallbackAction.APPROVE)
        assert replay.alert_text == "Caducada"
        assert gateway.approved == []

    async def test_non_spend_increase_approves_on_first_tap(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view(is_spend_increase=False)
        nonce = await _seed_nonce(store, action=CallbackAction.APPROVE)

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE)

        assert gateway.approved == [_PROPOSAL_ID]
        assert outcome.reply_markup == ()


class TestRejectAndSnooze:
    async def test_reject_calls_gateway_reject_with_live_diff_hash(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view(diff_hash="c" * 64)
        nonce = await _seed_nonce(store, action=CallbackAction.REJECT, diff_hash="c" * 64)

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.REJECT)

        assert gateway.rejected == [_PROPOSAL_ID]
        assert "Rechazado" in (outcome.edited_text or "")

    async def test_snooze_calls_gateway_postpone(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view()
        nonce = await _seed_nonce(store, action=CallbackAction.SNOOZE)

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.SNOOZE)

        assert gateway.postponed == [_PROPOSAL_ID]
        assert "Pospuesto 24 h" in (outcome.edited_text or "")

    async def test_denied_decision_shows_denial_never_a_success_label(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view(is_spend_increase=False)
        gateway.deny_next[_PROPOSAL_ID] = "GUARDRAIL_BLOCKED"
        nonce = await _seed_nonce(store, action=CallbackAction.APPROVE)

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE)

        assert "No se pudo aplicar" in outcome.alert_text
        assert "guardarraíl" in (outcome.edited_text or "")


class TestDetailDoesNotConsumeNonce:
    async def test_detail_can_be_tapped_more_than_once(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view()
        gateway.details[_PROPOSAL_ID] = _detail_view()
        nonce = await _seed_nonce(store, action=CallbackAction.DETAIL)

        first = await _tap(resolver, nonce=nonce, action=CallbackAction.DETAIL)
        second = await _tap(resolver, nonce=nonce, action=CallbackAction.DETAIL)

        assert first.alert_text == "Detalle"
        assert second.alert_text == "Detalle"
        assert first.reply_markup is None  # no toca el teclado ya enviado


class TestBatchByCause:
    async def test_single_member_group_approves_directly(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view()
        nonce = await _seed_nonce(store, action=CallbackAction.APPROVE, diff_hash=_DIFF_HASH)
        # is_spend_increase=True por defecto -> primer toque no aprueba
        await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE)
        confirm_nonce = _only_nonce_for(store, action=CallbackAction.CONFIRM)

        await _tap(resolver, nonce=confirm_nonce, action=CallbackAction.CONFIRM)

        assert gateway.approved == [_PROPOSAL_ID]

    async def test_batch_approves_independent_authorizations(self) -> None:
        resolver, store, gateway = _build()
        anchor_id = _PROPOSAL_ID
        second_id = "b" * 8 + "-0000-0000-0000-000000000000"
        third_id = "c" * 8 + "-0000-0000-0000-000000000000"
        gateway.live[anchor_id] = _view(proposal_id=anchor_id, entity_name="Secundaria Madrid")
        gateway.live[second_id] = _view(
            proposal_id=second_id, diff_hash="d" * 64, entity_name="Primaria Valencia"
        )
        gateway.live[third_id] = _view(
            proposal_id=third_id, diff_hash="e" * 64, entity_name="Inglés Andalucía"
        )
        gateway.groups[anchor_id] = (anchor_id, second_id, third_id)
        nonce = await _seed_nonce(store, action=CallbackAction.APPROVE, proposal_id=anchor_id)
        await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE, proposal_id=anchor_id)
        confirm_nonce = _only_nonce_for(store, action=CallbackAction.CONFIRM)

        outcome = await _tap(
            resolver, nonce=confirm_nonce, action=CallbackAction.CONFIRM, proposal_id=anchor_id
        )

        assert set(gateway.approved) == {anchor_id, second_id, third_id}
        assert outcome.alert_text == "Aprobadas"

    async def test_batch_reports_partial_failure_explicitly_never_silently(self) -> None:
        resolver, store, gateway = _build()
        anchor_id = _PROPOSAL_ID
        second_id = "b" * 8 + "-0000-0000-0000-000000000000"
        gateway.live[anchor_id] = _view(proposal_id=anchor_id, entity_name="Secundaria Madrid")
        gateway.live[second_id] = _view(
            proposal_id=second_id, diff_hash="d" * 64, entity_name="Primaria Valencia"
        )
        gateway.groups[anchor_id] = (anchor_id, second_id)
        gateway.deny_next[second_id] = "DIFF_CHANGED"
        nonce = await _seed_nonce(store, action=CallbackAction.APPROVE, proposal_id=anchor_id)
        await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE, proposal_id=anchor_id)
        confirm_nonce = _only_nonce_for(store, action=CallbackAction.CONFIRM)

        outcome = await _tap(
            resolver, nonce=confirm_nonce, action=CallbackAction.CONFIRM, proposal_id=anchor_id
        )

        assert gateway.approved == [anchor_id]
        assert outcome.alert_text == "1/2 aprobadas"
        assert outcome.show_alert is True
        text = outcome.edited_text or ""
        assert "Secundaria Madrid ✅" in text
        assert "Primaria Valencia ❌" in text
        assert "la propuesta cambió mientras se decidía" in text


class TestUndo:
    async def test_undo_within_grace_restores(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view()
        gateway.undo_results[_PROPOSAL_ID] = UndoResult(kind=UndoResultKind.RESTORED)
        nonce = await _seed_nonce(store, action=CallbackAction.UNDO)

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.UNDO)

        assert "restauró" in (outcome.edited_text or "")
        assert outcome.show_alert is False

    async def test_undo_after_grace_creates_compensating_proposal(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view()
        gateway.undo_results[_PROPOSAL_ID] = UndoResult(
            kind=UndoResultKind.COMPENSATING_PROPOSAL_CREATED
        )
        nonce = await _seed_nonce(store, action=CallbackAction.UNDO)

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.UNDO)

        assert "ventana de gracia" in (outcome.edited_text or "")

    async def test_undo_not_allowed_says_no_longer_available(self) -> None:
        resolver, store, gateway = _build()
        gateway.live[_PROPOSAL_ID] = _view()
        nonce = await _seed_nonce(store, action=CallbackAction.UNDO)
        # sin gateway.undo_results configurado: FakeApprovalGateway devuelve NOT_ALLOWED

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.UNDO)

        assert "ya no está disponible" in (outcome.edited_text or "")
        assert outcome.show_alert is True


class TestUnpairedChatNeverDecides:
    """contracts/telegram.md: "sin fila verificada el canal avisa pero no
    decide" -- allow-list y emparejamiento son comprobaciones distintas."""

    async def test_unpaired_chat_gets_denied_without_a_decision(self) -> None:
        guard = FakeTelegramPairingGuard(paired=False)
        resolver, store, gateway = _build(pairing_guard=guard)
        gateway.live[_PROPOSAL_ID] = _view(is_spend_increase=False)
        nonce = await _seed_nonce(store, action=CallbackAction.APPROVE)

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE)

        assert outcome.alert_text == "Sin emparejar"
        assert outcome.edited_text is None
        assert gateway.approved == []
        assert guard.denied_chat_ids == [_CHAT_ID]

    async def test_unpaired_chat_never_consumes_the_nonce(self) -> None:
        guard = FakeTelegramPairingGuard(paired=False)
        resolver, store, gateway = _build(pairing_guard=guard)
        gateway.live[_PROPOSAL_ID] = _view(is_spend_increase=False)
        nonce = await _seed_nonce(store, action=CallbackAction.APPROVE)

        await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE)
        guard.paired = True
        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE)

        assert gateway.approved == [_PROPOSAL_ID]  # solo la segunda pulsacion, ya emparejado
        assert outcome.alert_text != "Sin emparejar"

    async def test_paired_chat_decides_normally(self) -> None:
        resolver, store, gateway = _build(pairing_guard=FakeTelegramPairingGuard(paired=True))
        gateway.live[_PROPOSAL_ID] = _view(is_spend_increase=False)
        nonce = await _seed_nonce(store, action=CallbackAction.APPROVE)

        outcome = await _tap(resolver, nonce=nonce, action=CallbackAction.APPROVE)

        assert gateway.approved == [_PROPOSAL_ID]
        assert outcome.alert_text != "Sin emparejar"


def _detail_view() -> ApprovalDetailView:
    return ApprovalDetailView(
        evidence=(ApprovalEvidenceLine(metric="cpl", actual=19.0, target=28.0, window_label="7D"),),
        guardrail_floor_label="10 €",
        guardrail_ceiling_label="300 €",
        guardrail_monthly_cap_label="10.000 €",
    )


def _only_nonce_for(store: FakeTelegramCallbackStore, *, action: CallbackAction) -> str:
    matches = [
        nonce for nonce, row in store._rows.items() if row.action is action and not row.consumed
    ]
    assert len(matches) == 1, f"esperaba un nonce vivo para {action}, hay {len(matches)}"
    return matches[0]
