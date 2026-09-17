"""`ListPendingProposals` (este branch, `/pendientes`): reenvia hasta 10
tarjetas de aprobacion nivel 1, mas antiguas primero, con botones nuevos --
nunca reutiliza un nonce ya emitido (una tarjeta vieja pudo caducar o
resolverse desde entonces). Mismo camino que `PublishApprovalRequest`:
`ApprovalGatewayPort` para la vista viva, `TelegramCallbackStorePort` para
los nonces."""

from __future__ import annotations

import structlog

from safent_ads.notifications.application.errors import NotificationDeliveryError
from safent_ads.notifications.application.keyboards import approval_request_keyboard
from safent_ads.notifications.application.ports import (
    ApprovalGatewayPort,
    MessengerPort,
    PendingProposalIdsPort,
    TelegramCallbackStorePort,
    TelegramPairingGuardPort,
)
from safent_ads.notifications.application.rendering import (
    render_approval_request_card,
    render_no_pending_proposals,
)
from safent_ads.notifications.application.resolve_callback import to_approval_request_view
from safent_ads.notifications.domain.callback import CallbackAction, callback_ttl, generate_nonce
from safent_ads.shared.clock import Clock

logger = structlog.get_logger(__name__)

MAX_PENDING_LISTED = 10
_UNPAIRED_REPLY = "Sin emparejar"
_KEYBOARD_ACTIONS = (
    CallbackAction.APPROVE,
    CallbackAction.REJECT,
    CallbackAction.SNOOZE,
    CallbackAction.DETAIL,
)


class ListPendingProposals:
    def __init__(
        self,
        *,
        pending_ids: PendingProposalIdsPort,
        gateway: ApprovalGatewayPort,
        callback_store: TelegramCallbackStorePort,
        pairing_guard: TelegramPairingGuardPort,
        messenger: MessengerPort,
        clock: Clock,
    ) -> None:
        self._pending_ids = pending_ids
        self._gateway = gateway
        self._callback_store = callback_store
        self._pairing_guard = pairing_guard
        self._messenger = messenger
        self._clock = clock

    async def execute(self, *, chat_id: int) -> int:
        if not await self._pairing_guard.is_chat_paired(chat_id):
            await self._messenger.send(chat_id=chat_id, text=_UNPAIRED_REPLY)
            return 0
        ids = await self._pending_ids.list_pending_ids(limit=MAX_PENDING_LISTED)
        if not ids:
            await self._messenger.send(chat_id=chat_id, text=render_no_pending_proposals())
            return 0
        sent = 0
        for proposal_id in ids:
            if await self._send_card(chat_id=chat_id, proposal_id=proposal_id):
                sent += 1
        return sent

    async def _send_card(self, *, chat_id: int, proposal_id: str) -> bool:
        live = await self._gateway.get_live_proposal(proposal_id)
        if live is None or not live.is_pending:
            return False
        view = to_approval_request_view(live)
        nonces = {action: generate_nonce() for action in _KEYBOARD_ACTIONS}
        keyboard = approval_request_keyboard(
            proposal_id=live.proposal_id,
            approve_nonce=nonces[CallbackAction.APPROVE],
            reject_nonce=nonces[CallbackAction.REJECT],
            snooze_nonce=nonces[CallbackAction.SNOOZE],
            detail_nonce=nonces[CallbackAction.DETAIL],
        )
        try:
            message_id = await self._messenger.send(
                chat_id=chat_id, text=render_approval_request_card(view), reply_markup=keyboard
            )
        except NotificationDeliveryError:
            logger.warning("pending_proposals_card_delivery_failed", proposal_id=proposal_id)
            return False
        expires_at = callback_ttl(proposal_expires_at=live.expires_at, now=self._clock.now())
        for action, nonce in nonces.items():
            await self._callback_store.create(
                nonce=nonce,
                proposal_id=live.proposal_id,
                chat_id=chat_id,
                message_id=message_id,
                diff_hash=live.diff_hash,
                action=action,
                expires_at=expires_at,
            )
        return True
