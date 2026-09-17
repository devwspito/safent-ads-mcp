"""`ResolveCallback` (plan.md §5: "notifications: ... ResolveCallback"): la
maquina de estados que decide que hace una pulsacion de boton, ya
autenticada por `TelegramOwnerAllowList` (infra). Puro respecto a Telegram y
a `proposals`/`execution`: solo conoce los puertos declarados en
`application/ports.py` -- la implementacion real de `ApprovalGatewayPort`
(`notifications/infrastructure/proposal_approval_gateway.py`) es quien
llama a `SubmitApproval`, nunca esta clase.

Reglas de contracts/telegram.md que aplica:
1. Nonce desconocido/caducado/consumido -> "Caducada" + reedita con el
   estado real.
2. `diff_hash` del nonce != vivo (INV-1) -> "La propuesta cambio" + nonce
   nuevo.
3. Siempre se responde al callback (el llamador -- `AiogramMessenger` --
   hace el `answerCallbackQuery`, aqui solo se construye el texto/alerta).
4. Resuelta la decision: mensaje editado con el desenlace, botones fuera.
5. TTL del nonce = min(proposal.expires_at, now + 6h).

Segundo toque para SUBIR: el primer `a` sobre una propuesta que aumenta
gasto no aprueba -- emite un nonce `c` nuevo y pinta la tarjeta de
confirmacion. Lote por causa: `a`/`c` sobre una propuesta con hermanas
`PENDING` de la misma causa (`ApprovalGatewayPort.get_group_members`)
aprueba las N de forma independiente, nunca como una unica autorizacion."""

from __future__ import annotations

from datetime import datetime

from safent_ads.notifications.application.dto import ApprovalRequestView, ConfirmSpendIncreaseView
from safent_ads.notifications.application.keyboards import (
    approval_request_keyboard,
    confirm_spend_increase_keyboard,
)
from safent_ads.notifications.application.ports import (
    ApprovalGatewayPort,
    CallbackOutcome,
    DecisionKind,
    DecisionResult,
    InlineKeyboard,
    LiveProposalView,
    TelegramCallbackRecord,
    TelegramCallbackStorePort,
    TelegramPairingGuardPort,
    UndoResultKind,
)
from safent_ads.notifications.application.rendering import (
    render_approval_detail_card,
    render_approval_request_card,
    render_batch_outcome,
    render_confirm_spend_increase_card,
    render_decision_outcome,
    render_denial_notice,
    render_diff_changed_notice,
    render_expired_notice,
    render_undo_outcome,
)
from safent_ads.notifications.domain.callback import (
    CallbackAction,
    CallbackData,
    callback_ttl,
    generate_nonce,
)
from safent_ads.notifications.domain.errors import InvalidCallbackDataError
from safent_ads.shared.clock import Clock

_SNOOZE_HOURS = 24
_EMPTY_KEYBOARD: InlineKeyboard = ()
_KEYBOARD_ACTIONS = (
    CallbackAction.APPROVE,
    CallbackAction.REJECT,
    CallbackAction.SNOOZE,
    CallbackAction.DETAIL,
)

_DECISION_LABELS: dict[DecisionKind, str] = {
    DecisionKind.APPROVED: "✅ Aprobado",
    DecisionKind.REJECTED: "❌ Rechazado",
    DecisionKind.POSTPONED: "⏸ Pospuesto 24 h",
}

_DENIAL_LABELS: dict[str, str] = {
    "DIFF_CHANGED": "la propuesta cambió mientras se decidía",
    "PROPOSAL_EXPIRED": "la propuesta ya caducó",
    "PROPOSAL_NOT_PENDING": "la propuesta ya no está pendiente",
    "BRAKE_ENGAGED": "el freno de emergencia está activado",
    "GUARDRAIL_BLOCKED": "un guardarraíl lo bloquea",
}


_UNPAIRED_OUTCOME = CallbackOutcome(alert_text="Sin emparejar", show_alert=True)


class ResolveCallback:
    def __init__(
        self,
        *,
        store: TelegramCallbackStorePort,
        gateway: ApprovalGatewayPort,
        pairing_guard: TelegramPairingGuardPort,
        clock: Clock,
    ) -> None:
        self._store = store
        self._gateway = gateway
        self._pairing_guard = pairing_guard
        self._clock = clock

    async def execute(
        self, *, callback_data: str, chat_id: int, from_user_id: int, message_id: int
    ) -> CallbackOutcome:
        denial = await self._deny_if_unpaired(chat_id)
        if denial is not None:
            return denial
        try:
            data = CallbackData.parse(callback_data)
        except InvalidCallbackDataError:
            return _malformed_outcome()

        if data.action is CallbackAction.DETAIL:
            return await self._resolve_detail(data.nonce, chat_id=chat_id, message_id=message_id)

        return await self._resolve_decision(
            data, chat_id=chat_id, from_user_id=from_user_id, message_id=message_id
        )

    async def _resolve_decision(
        self, data: CallbackData, *, chat_id: int, from_user_id: int, message_id: int
    ) -> CallbackOutcome:
        now = self._clock.now()
        record = await self._store.consume(
            nonce=data.nonce, chat_id=chat_id, message_id=message_id, now=now
        )
        if record is None:
            return await self._render_stale(data.nonce)

        live = await self._gateway.get_live_proposal(record.proposal_id)
        if live is None or not live.is_pending:
            return self._render_terminal_state(record, live)
        if live.diff_hash != record.diff_hash:
            return await self._reissue_on_diff_change(record, live, now)

        decided_by = f"telegram:{from_user_id}"
        return await self._dispatch(record, live, decided_by=decided_by, now=now)

    async def _deny_if_unpaired(self, chat_id: int) -> CallbackOutcome | None:
        """contracts/telegram.md: "sin fila verificada el canal avisa pero
        no decide" -- allow-list (adaptador) y emparejamiento vivo (esto)
        son dos comprobaciones distintas."""
        if await self._pairing_guard.is_chat_paired(chat_id):
            return None
        await self._pairing_guard.record_unpaired_callback(chat_id)
        return _UNPAIRED_OUTCOME

    # ------------------------------------------------------------------
    # Regla 1: nonce desconocido/caducado/consumido.
    # ------------------------------------------------------------------

    async def _render_stale(self, nonce: str) -> CallbackOutcome:
        found = await self._store.find_by_nonce(nonce)
        if found is None:
            return _malformed_outcome()
        live = await self._gateway.get_live_proposal(found.proposal_id)
        entity_name = live.entity_name if live is not None else "esta propuesta"
        state_label = live.state_label if live is not None else "desconocido"
        text = render_expired_notice(entity_name=entity_name, state_label=state_label)
        return CallbackOutcome(
            alert_text="Caducada", show_alert=False, edited_text=text, reply_markup=_EMPTY_KEYBOARD
        )

    def _render_terminal_state(
        self, record: TelegramCallbackRecord, live: LiveProposalView | None
    ) -> CallbackOutcome:
        entity_name = live.entity_name if live is not None else "esta propuesta"
        state_label = live.state_label if live is not None else "resuelto"
        del record
        text = render_expired_notice(entity_name=entity_name, state_label=state_label)
        return CallbackOutcome(
            alert_text="Ya no está pendiente",
            show_alert=True,
            edited_text=text,
            reply_markup=_EMPTY_KEYBOARD,
        )

    # ------------------------------------------------------------------
    # Regla 2 / INV-1: la propuesta cambio desde que se pinto la tarjeta.
    # ------------------------------------------------------------------

    async def _reissue_on_diff_change(
        self, record: TelegramCallbackRecord, live: LiveProposalView, now: datetime
    ) -> CallbackOutcome:
        nonces = {action: generate_nonce() for action in _KEYBOARD_ACTIONS}
        expires_at = callback_ttl(proposal_expires_at=live.expires_at, now=now)
        for action, nonce in nonces.items():
            await self._store.create(
                nonce=nonce,
                proposal_id=live.proposal_id,
                chat_id=record.chat_id,
                message_id=record.message_id,
                diff_hash=live.diff_hash,
                action=action,
                expires_at=expires_at,
            )
        keyboard = approval_request_keyboard(
            proposal_id=live.proposal_id,
            approve_nonce=nonces[CallbackAction.APPROVE],
            reject_nonce=nonces[CallbackAction.REJECT],
            snooze_nonce=nonces[CallbackAction.SNOOZE],
            detail_nonce=nonces[CallbackAction.DETAIL],
        )
        text = (
            f"{render_diff_changed_notice(entity_name=live.entity_name)}\n\n"
            f"{render_approval_request_card(to_approval_request_view(live))}"
        )
        return CallbackOutcome(
            alert_text="La propuesta cambió",
            show_alert=True,
            edited_text=text,
            reply_markup=keyboard,
        )

    # ------------------------------------------------------------------
    # `[Detalle]`: no consume el nonce de aprobacion.
    # ------------------------------------------------------------------

    async def _resolve_detail(
        self, nonce: str, *, chat_id: int, message_id: int
    ) -> CallbackOutcome:
        now = self._clock.now()
        record = await self._store.peek(
            nonce=nonce, chat_id=chat_id, message_id=message_id, now=now
        )
        if record is None:
            return await self._render_stale(nonce)
        live = await self._gateway.get_live_proposal(record.proposal_id)
        if live is None:
            return self._render_terminal_state(record, live)
        detail = await self._gateway.get_detail(record.proposal_id)
        if detail is None:
            return CallbackOutcome(
                alert_text="Sin detalle disponible", show_alert=True, edited_text=None
            )
        view = to_approval_request_view(live)
        text = render_approval_detail_card(view, detail)
        return CallbackOutcome(alert_text="Detalle", show_alert=False, edited_text=text)

    # ------------------------------------------------------------------
    # Dispatch de accion ya validada (nonce consumido, diff_hash vivo).
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        record: TelegramCallbackRecord,
        live: LiveProposalView,
        *,
        decided_by: str,
        now: datetime,
    ) -> CallbackOutcome:
        if record.action is CallbackAction.APPROVE:
            return await self._handle_approve_tap(record, live, decided_by=decided_by, now=now)
        if record.action is CallbackAction.CONFIRM:
            return await self._approve_group_or_single(live.proposal_id, decided_by=decided_by)
        if record.action is CallbackAction.REJECT:
            result = await self._gateway.reject(
                proposal_id=record.proposal_id, diff_hash=live.diff_hash, decided_by=decided_by
            )
            return self._render_single_decision(live, result, decided_by)
        if record.action is CallbackAction.SNOOZE:
            result = await self._gateway.postpone(
                proposal_id=record.proposal_id, decided_by=decided_by, hours=_SNOOZE_HOURS
            )
            return self._render_single_decision(live, result, decided_by)
        result_undo = await self._gateway.undo(
            proposal_id=record.proposal_id, initiated_by=decided_by
        )
        text = render_undo_outcome(entity_name=live.entity_name, kind=result_undo.kind)
        return CallbackOutcome(
            alert_text=_UNDO_ALERTS[result_undo.kind],
            show_alert=result_undo.kind in _UNDO_ALERT_ONLY_KINDS,
            edited_text=text,
            reply_markup=_EMPTY_KEYBOARD,
        )

    async def _handle_approve_tap(
        self,
        record: TelegramCallbackRecord,
        live: LiveProposalView,
        *,
        decided_by: str,
        now: datetime,
    ) -> CallbackOutcome:
        if not live.is_spend_increase:
            return await self._approve_group_or_single(live.proposal_id, decided_by=decided_by)
        return await self._issue_confirm_card(record, live, now)

    async def _issue_confirm_card(
        self, record: TelegramCallbackRecord, live: LiveProposalView, now: datetime
    ) -> CallbackOutcome:
        expires_at = callback_ttl(proposal_expires_at=live.expires_at, now=now)
        confirm_nonce = generate_nonce()
        await self._store.create(
            nonce=confirm_nonce,
            proposal_id=live.proposal_id,
            chat_id=record.chat_id,
            message_id=record.message_id,
            diff_hash=live.diff_hash,
            action=CallbackAction.CONFIRM,
            expires_at=expires_at,
        )
        cancel_nonce = generate_nonce()
        await self._store.create(
            nonce=cancel_nonce,
            proposal_id=live.proposal_id,
            chat_id=record.chat_id,
            message_id=record.message_id,
            diff_hash=live.diff_hash,
            action=CallbackAction.REJECT,
            expires_at=expires_at,
        )
        detail = await self._gateway.get_detail(live.proposal_id)
        monthly_cap_label = detail.guardrail_monthly_cap_label if detail is not None else "—"
        view = ConfirmSpendIncreaseView(
            entity_name=live.entity_name,
            before_label=live.before_label,
            after_label=live.after_label,
            impact_label=live.impact_label,
            monthly_cap_label=monthly_cap_label,
        )
        keyboard = confirm_spend_increase_keyboard(
            proposal_id=live.proposal_id, confirm_nonce=confirm_nonce, cancel_nonce=cancel_nonce
        )
        text = render_confirm_spend_increase_card(view)
        return CallbackOutcome(
            alert_text="Confirma la subida",
            show_alert=False,
            edited_text=text,
            reply_markup=keyboard,
        )

    async def _approve_group_or_single(
        self, anchor_proposal_id: str, *, decided_by: str
    ) -> CallbackOutcome:
        members = await self._gateway.get_group_members(anchor_proposal_id)
        if not members:
            return _malformed_outcome()
        if len(members) == 1:
            anchor = members[0]
            result = await self._gateway.approve(
                proposal_id=anchor.proposal_id, diff_hash=anchor.diff_hash, decided_by=decided_by
            )
            return self._render_single_decision(anchor, result, decided_by)
        return await self._approve_batch(members, decided_by=decided_by)

    async def _approve_batch(
        self, members: tuple[LiveProposalView, ...], *, decided_by: str
    ) -> CallbackOutcome:
        results: list[tuple[str, bool, str | None]] = []
        for member in members:
            outcome = await self._gateway.approve(
                proposal_id=member.proposal_id, diff_hash=member.diff_hash, decided_by=decided_by
            )
            ok = outcome.kind is DecisionKind.APPROVED
            reason = None if ok else _denial_label(outcome.denial_reason)
            results.append((member.entity_name, ok, reason))

        approved_count = sum(1 for _, ok, _ in results if ok)
        text = render_batch_outcome(cause_text=members[0].cause_text, results=results)
        all_ok = approved_count == len(results)
        alert = "Aprobadas" if all_ok else f"{approved_count}/{len(results)} aprobadas"
        return CallbackOutcome(
            alert_text=alert, show_alert=not all_ok, edited_text=text, reply_markup=_EMPTY_KEYBOARD
        )

    def _render_single_decision(
        self, live: LiveProposalView, result: DecisionResult, decided_by: str
    ) -> CallbackOutcome:
        if result.kind is DecisionKind.DENIED:
            text = render_denial_notice(
                entity_name=live.entity_name, reason_label=_denial_label(result.denial_reason)
            )
            return CallbackOutcome(
                alert_text="No se pudo aplicar",
                show_alert=True,
                edited_text=text,
                reply_markup=_EMPTY_KEYBOARD,
            )
        label = _DECISION_LABELS[result.kind]
        text = render_decision_outcome(
            entity_name=live.entity_name,
            decision_label=label,
            decided_by=decided_by,
            decided_at=self._clock.now(),
        )
        return CallbackOutcome(
            alert_text=label, show_alert=False, edited_text=text, reply_markup=_EMPTY_KEYBOARD
        )


_UNDO_ALERTS: dict[UndoResultKind, str] = {
    UndoResultKind.CANCELLED: "Cancelado",
    UndoResultKind.RESTORED: "Deshecho",
    UndoResultKind.COMPENSATING_PROPOSAL_CREATED: "Fuera de la ventana de gracia",
    UndoResultKind.NOT_ALLOWED: "Deshacer no disponible",
    UndoResultKind.ALREADY_UNDONE: "Ya se deshizo",
}
# Los dos desenlaces sin cambio de estado real (nunca hubo nada que
# deshacer, o ya se deshizo antes): Telegram los muestra como alerta modal
# en vez de solo editar el texto de la tarjeta, igual que un error.
_UNDO_ALERT_ONLY_KINDS: frozenset[UndoResultKind] = frozenset(
    {UndoResultKind.NOT_ALLOWED, UndoResultKind.ALREADY_UNDONE}
)


def _denial_label(reason: str | None) -> str:
    if reason is None:
        return "motivo desconocido"
    return _DENIAL_LABELS.get(reason, reason.lower())


def _malformed_outcome() -> CallbackOutcome:
    return CallbackOutcome(alert_text="Caducada", show_alert=False)


def to_approval_request_view(live: LiveProposalView) -> ApprovalRequestView:
    return ApprovalRequestView(
        proposal_id=live.proposal_id,
        diff_hash=live.diff_hash,
        entity_name=live.entity_name,
        platform_label=live.platform_label,
        kind=live.kind,
        parameter_label=live.parameter_label,
        before_label=live.before_label,
        after_label=live.after_label,
        change_note=live.change_note,
        cause_text=live.cause_text,
        window_label=live.window_label,
        impact_label=live.impact_label,
        rule_id=live.rule_id,
        expires_at=live.expires_at,
        is_spend_increase=live.is_spend_increase,
    )
