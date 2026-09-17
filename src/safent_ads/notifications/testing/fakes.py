"""Dobles en memoria de los puertos de `notifications/application/ports.py`
(plan.md: "in-memory fakes en <context>/testing/, la integracion es un paso
de cableado posterior")."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime

from safent_ads.notifications.application.dto import (
    ApprovalDetailView,
    TickerSignal,
)
from safent_ads.notifications.application.errors import NotificationDeliveryError
from safent_ads.notifications.application.ports import (
    BrakeConfirmationRecord,
    BrakeStatusView,
    BrakeToggleOutcomeKind,
    BrakeToggleResult,
    BusinessStatusView,
    BusinessSummaryView,
    DecisionKind,
    DecisionResult,
    InlineKeyboard,
    LiveProposalView,
    PairingSnapshot,
    PendingPairingCandidate,
    PendingTestMessage,
    TelegramCallbackRecord,
    UndoResult,
    UndoResultKind,
)
from safent_ads.notifications.domain.brake_confirmation import PendingBrakeAction
from safent_ads.notifications.domain.callback import CallbackAction
from safent_ads.notifications.domain.dedupe_guard import DuplicateSendGuard
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.pairing import PairingCode, PairingStatus, effective_status
from safent_ads.shared.ids import BusinessId


@dataclass
class SentMessage:
    chat_id: int
    text: str
    disable_notification: bool
    reply_markup: InlineKeyboard | None = None


class FakeMessenger:
    """`MessengerPort` en memoria. `fail_next` fuerza el siguiente `send` a
    lanzar `NotificationDeliveryError`, para probar el camino de fallo sin
    tocar Telegram."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.edited: list[tuple[int, int, str, InlineKeyboard | None]] = []
        self._next_message_id = 1
        self.fail_next = False

    async def send(
        self,
        *,
        chat_id: int,
        text: str,
        disable_notification: bool = False,
        reply_markup: InlineKeyboard | None = None,
    ) -> int:
        if self.fail_next:
            self.fail_next = False
            raise NotificationDeliveryError("fallo simulado de entrega")
        self.sent.append(SentMessage(chat_id, text, disable_notification, reply_markup))
        message_id = self._next_message_id
        self._next_message_id += 1
        return message_id

    async def edit(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboard | None = None,
    ) -> None:
        self.edited.append((chat_id, message_id, text, reply_markup))


class FakeSignalsForTicker:
    def __init__(self, by_business: dict[BusinessId, list[TickerSignal]] | None = None) -> None:
        self._by_business = by_business or {}

    def set_signals(self, business_id: BusinessId, signals: list[TickerSignal]) -> None:
        self._by_business[business_id] = signals

    async def list_actionable_signals(self, business_id: BusinessId) -> list[TickerSignal]:
        return list(self._by_business.get(business_id, []))


class FakeNotificationOutbox:
    """Envuelve `DuplicateSendGuard` (dominio) y ademas guarda cada
    notificacion, como haria la tabla real (T043, fuera de esta lane)."""

    def __init__(self) -> None:
        self._guard = DuplicateSendGuard()
        self.saved: list[Notification] = []

    async def try_reserve(self, notification: Notification) -> bool:
        return self._guard.register(notification.dedupe_key)

    async def save(self, notification: Notification) -> None:
        self.saved.append(notification)


@dataclass
class _PendingEntry:
    signals: list[TickerSignal]
    queued_at: datetime


class FakePendingDigest:
    def __init__(self) -> None:
        self._queues: dict[BusinessId, list[_PendingEntry]] = {}

    async def enqueue(
        self, business_id: BusinessId, signals: list[TickerSignal], *, queued_at: datetime
    ) -> None:
        self._queues.setdefault(business_id, []).append(_PendingEntry(signals, queued_at))

    async def pop_due(self, business_id: BusinessId, *, at: datetime) -> list[TickerSignal]:
        """Solo vacia lo encolado hasta `at` (inclusive): una senal
        encolada "en el futuro" respecto al momento consultado se queda en
        la cola, igual que haria una tabla real particionada por tiempo."""
        entries = self._queues.get(business_id, [])
        due = [entry for entry in entries if entry.queued_at <= at]
        still_pending = [entry for entry in entries if entry.queued_at > at]
        self._set_queue(business_id, still_pending)

        merged: list[TickerSignal] = []
        for entry in due:
            merged.extend(entry.signals)
        return merged

    def _set_queue(self, business_id: BusinessId, entries: list[_PendingEntry]) -> None:
        if entries:
            self._queues[business_id] = entries
        else:
            self._queues.pop(business_id, None)


@dataclass
class _CallbackRow:
    proposal_id: str
    chat_id: int
    message_id: int
    diff_hash: str
    action: CallbackAction
    expires_at: datetime
    consumed: bool = False


class FakeTelegramCallbackStore:
    """`TelegramCallbackStorePort` en memoria: `consume` es de un solo uso
    (contracts/telegram.md), `peek` no marca nada -- mismo contrato que
    `SqlTelegramCallbackStore` sobre `telegram_callbacks`."""

    def __init__(self) -> None:
        self._rows: dict[str, _CallbackRow] = {}

    async def create(
        self,
        *,
        nonce: str,
        proposal_id: str,
        chat_id: int,
        message_id: int,
        diff_hash: str,
        action: CallbackAction,
        expires_at: datetime,
    ) -> None:
        self._rows[nonce] = _CallbackRow(
            proposal_id=proposal_id,
            chat_id=chat_id,
            message_id=message_id,
            diff_hash=diff_hash,
            action=action,
            expires_at=expires_at,
        )

    async def consume(
        self, *, nonce: str, chat_id: int, message_id: int, now: datetime
    ) -> TelegramCallbackRecord | None:
        row = self._live_row(nonce, chat_id=chat_id, message_id=message_id, now=now)
        if row is None or row.consumed:
            return None
        row.consumed = True
        return _row_to_record(nonce, row)

    async def peek(
        self, *, nonce: str, chat_id: int, message_id: int, now: datetime
    ) -> TelegramCallbackRecord | None:
        row = self._live_row(nonce, chat_id=chat_id, message_id=message_id, now=now)
        return None if row is None else _row_to_record(nonce, row)

    async def find_by_nonce(self, nonce: str) -> TelegramCallbackRecord | None:
        row = self._rows.get(nonce)
        return None if row is None else _row_to_record(nonce, row)

    def _live_row(
        self, nonce: str, *, chat_id: int, message_id: int, now: datetime
    ) -> _CallbackRow | None:
        row = self._rows.get(nonce)
        if row is None:
            return None
        if row.chat_id != chat_id or row.message_id != message_id or row.expires_at <= now:
            return None
        return row


def _row_to_record(nonce: str, row: _CallbackRow) -> TelegramCallbackRecord:
    return TelegramCallbackRecord(
        nonce=nonce,
        proposal_id=row.proposal_id,
        chat_id=row.chat_id,
        message_id=row.message_id,
        diff_hash=row.diff_hash,
        action=row.action,
    )


class FakeApprovalGateway:
    """`ApprovalGatewayPort` en memoria: para probar `ResolveCallback` sin
    tocar `proposals`/`execution` ni una base de datos. `deny_next` fuerza
    la siguiente `approve()`/`reject()`/`postpone()` de una propuesta a
    denegarse una vez, para probar fallos parciales de lote."""

    def __init__(self) -> None:
        self.live: dict[str, LiveProposalView] = {}
        self.details: dict[str, ApprovalDetailView] = {}
        self.groups: dict[str, tuple[str, ...]] = {}
        self.deny_next: dict[str, str] = {}
        self.undo_results: dict[str, UndoResult] = {}
        self.approved: list[str] = []
        self.rejected: list[str] = []
        self.postponed: list[str] = []

    async def get_live_proposal(self, proposal_id: str) -> LiveProposalView | None:
        return self.live.get(proposal_id)

    async def get_group_members(self, anchor_proposal_id: str) -> tuple[LiveProposalView, ...]:
        member_ids = self.groups.get(anchor_proposal_id, (anchor_proposal_id,))
        return tuple(self.live[pid] for pid in member_ids if pid in self.live)

    async def get_detail(self, proposal_id: str) -> ApprovalDetailView | None:
        return self.details.get(proposal_id)

    async def approve(self, *, proposal_id: str, diff_hash: str, decided_by: str) -> DecisionResult:
        del diff_hash, decided_by
        denial = self.deny_next.pop(proposal_id, None)
        if denial is not None:
            return DecisionResult(kind=DecisionKind.DENIED, denial_reason=denial)
        self.approved.append(proposal_id)
        self._mark_resolved(proposal_id)
        return DecisionResult(kind=DecisionKind.APPROVED)

    async def reject(self, *, proposal_id: str, diff_hash: str, decided_by: str) -> DecisionResult:
        del diff_hash, decided_by
        self.rejected.append(proposal_id)
        self._mark_resolved(proposal_id)
        return DecisionResult(kind=DecisionKind.REJECTED)

    async def postpone(
        self, *, proposal_id: str, decided_by: str, hours: int
    ) -> DecisionResult:
        del decided_by, hours
        self.postponed.append(proposal_id)
        self._mark_resolved(proposal_id)
        return DecisionResult(kind=DecisionKind.POSTPONED)

    async def undo(self, *, proposal_id: str, initiated_by: str) -> UndoResult:
        del initiated_by
        return self.undo_results.get(proposal_id, UndoResult(kind=UndoResultKind.NOT_ALLOWED))

    def _mark_resolved(self, proposal_id: str) -> None:
        view = self.live.get(proposal_id)
        if view is not None:
            self.live[proposal_id] = replace(view, is_pending=False, state_label="resuelto")


class FakeTelegramPairingGuard:
    """`TelegramPairingGuardPort` en memoria. `paired=True` por defecto:
    los tests de `ResolveCallback` que no versan sobre emparejamiento no
    tienen que preocuparse por el (contracts/telegram.md: la comprobacion
    es nueva, pero el resto de reglas del canal no dependen de ella)."""

    def __init__(self, *, paired: bool = True) -> None:
        self.paired = paired
        self.denied_chat_ids: list[int] = []
        self.denied_commands: list[tuple[int, str]] = []

    async def is_chat_paired(self, chat_id: int) -> bool:
        del chat_id
        return self.paired

    async def record_unpaired_callback(self, chat_id: int) -> None:
        self.denied_chat_ids.append(chat_id)

    async def record_unpaired_command_denied(self, chat_id: int, *, command: str) -> None:
        self.denied_commands.append((chat_id, command))


@dataclass
class _PairingRow:
    status: PairingStatus = PairingStatus.UNPAIRED
    chat_id: int | None = None
    verified_at: datetime | None = None
    code_hash: str | None = None
    code_expires_at: datetime | None = None
    pairing_code: str | None = None
    last_test_at: datetime | None = None


class FakeTelegramPairingRepository:
    """`TelegramPairingRepositoryPort` en memoria: lado del propietario
    (siempre por `owner_id`), mismo comportamiento que `SqlTelegramPairing
    Repository` sin cifrado real -- `pairing_code` viaja en claro tal cual
    se genero, ya que aqui no hay tabla que proteger."""

    def __init__(self) -> None:
        self._rows: dict[uuid.UUID, _PairingRow] = {}

    async def get(self, owner_id: uuid.UUID, *, now: datetime) -> PairingSnapshot | None:
        row = self._rows.get(owner_id)
        if row is None:
            return None
        status = effective_status(row.status, code_expires_at=row.code_expires_at, now=now)
        return PairingSnapshot(
            status=status,
            chat_id=row.chat_id if status is PairingStatus.PAIRED else None,
            paired_at=row.verified_at if status is PairingStatus.PAIRED else None,
            code_expires_at=row.code_expires_at if status is PairingStatus.PENDING else None,
            pairing_code=row.pairing_code if status is PairingStatus.PENDING else None,
            last_test_at=row.last_test_at,
        )

    async def start_pairing(
        self, *, owner_id: uuid.UUID, code: PairingCode, expires_at: datetime
    ) -> None:
        self._rows[owner_id] = _PairingRow(
            status=PairingStatus.PENDING,
            code_hash=code.hash(),
            code_expires_at=expires_at,
            pairing_code=code.value,
        )

    async def unpair(self, owner_id: uuid.UUID) -> None:
        self._rows[owner_id] = _PairingRow(status=PairingStatus.UNPAIRED)

    async def record_test_message_sent(self, owner_id: uuid.UUID, *, at: datetime) -> None:
        row = self._rows.get(owner_id)
        if row is not None:
            row.last_test_at = at

    # --- ayudantes de test, no parte del puerto ---

    def seed_paired(self, owner_id: uuid.UUID, *, chat_id: int, verified_at: datetime) -> None:
        self._rows[owner_id] = _PairingRow(
            status=PairingStatus.PAIRED, chat_id=chat_id, verified_at=verified_at
        )


class FakeTelegramPairingConfirmation:
    """`TelegramPairingConfirmationPort` en memoria: lado del bot,
    resuelto por CODIGO."""

    def __init__(self) -> None:
        self.candidates: list[PendingPairingCandidate] = []
        self.confirmed: list[tuple[uuid.UUID, int]] = []

    async def list_pending(self, *, now: datetime) -> list[PendingPairingCandidate]:
        del now
        return list(self.candidates)

    async def confirm(self, *, owner_id: uuid.UUID, chat_id: int, at: datetime) -> None:
        del at
        self.confirmed.append((owner_id, chat_id))


class FakeTelegramPairingAttempts:
    """`TelegramPairingAttemptsPort` en memoria (limite de 3/hora)."""

    def __init__(self) -> None:
        self._attempts: list[tuple[int, bool, datetime]] = []

    async def count_recent(self, chat_id: int, *, since: datetime) -> int:
        return sum(
            1 for cid, _succeeded, at in self._attempts if cid == chat_id and at >= since
        )

    async def record(self, chat_id: int, *, succeeded: bool, at: datetime) -> None:
        self._attempts.append((chat_id, succeeded, at))


class FakeTestMessageOutbox:
    """`TestMessageOutboxPort` en memoria. `_enqueued_at` respalda
    `count_recent` (freno de `POST /telegram/pairing/test-message`,
    security-review-f4.md item 1)."""

    def __init__(self) -> None:
        self._pending: dict[uuid.UUID, PendingTestMessage] = {}
        self._enqueued_at: list[tuple[uuid.UUID, datetime]] = []
        self.sent: list[uuid.UUID] = []
        self.failed: list[uuid.UUID] = []

    async def enqueue(
        self,
        *,
        notification_id: uuid.UUID,
        owner_id: uuid.UUID,
        chat_id: int,
        body: str,
        at: datetime,
    ) -> None:
        self._pending[notification_id] = PendingTestMessage(
            notification_id=notification_id, chat_id=chat_id, body=body
        )
        self._enqueued_at.append((owner_id, at))

    async def count_recent(self, owner_id: uuid.UUID, *, since: datetime) -> int:
        return sum(1 for owner, at in self._enqueued_at if owner == owner_id and at >= since)

    async def claim_pending(self, *, limit: int) -> list[PendingTestMessage]:
        return list(self._pending.values())[:limit]

    async def mark_sent(self, notification_id: uuid.UUID, *, platform_message_id: int) -> None:
        del platform_message_id
        self._pending.pop(notification_id, None)
        self.sent.append(notification_id)

    async def mark_failed(self, notification_id: uuid.UUID) -> None:
        self._pending.pop(notification_id, None)
        self.failed.append(notification_id)


class FakePendingProposalIds:
    """`PendingProposalIdsPort` en memoria: `ids` ya viene en el orden que
    el test quiera simular (mas antiguas primero, contracts/telegram.md)."""

    def __init__(self, ids: tuple[str, ...] = ()) -> None:
        self.ids = ids

    async def list_pending_ids(self, *, limit: int) -> tuple[str, ...]:
        return self.ids[:limit]


class FakeBusinessStatusPort:
    """`BusinessStatusPort` en memoria para `/estado`."""

    def __init__(
        self,
        businesses: tuple[BusinessSummaryView, ...] = (),
        statuses: dict[str, BusinessStatusView] | None = None,
    ) -> None:
        self.businesses = businesses
        self.statuses = statuses or {}

    async def list_businesses(self) -> tuple[BusinessSummaryView, ...]:
        return self.businesses

    async def get_status(self, business_id: BusinessId) -> BusinessStatusView:
        return self.statuses[str(business_id)]


class FakeBrakeGateway:
    """`BrakeGatewayPort` en memoria: para probar `/freno` sin `execution`
    ni una base de datos. `engaged`/`mode`/`reason`/`since` reflejan el
    estado actual, mismos campos que `EmergencyBrake`."""

    def __init__(self) -> None:
        self.engaged = False
        self.mode: str | None = None
        self.reason: str | None = None
        self.since: datetime | None = None
        self.engage_calls: list[str] = []
        self.release_calls: list[str] = []

    async def get_status(self) -> BrakeStatusView:
        return BrakeStatusView(
            engaged=self.engaged, mode=self.mode, reason=self.reason, since=self.since
        )

    async def engage(self, *, reason: str, engaged_by: str) -> BrakeToggleResult:
        if self.engaged:
            return BrakeToggleResult(kind=BrakeToggleOutcomeKind.ALREADY_ENGAGED)
        self.engaged = True
        self.mode = "all"
        self.reason = reason
        self.engage_calls.append(engaged_by)
        return BrakeToggleResult(
            kind=BrakeToggleOutcomeKind.ENGAGED, status=await self.get_status()
        )

    async def release(self, *, released_by: str) -> BrakeToggleResult:
        if not self.engaged:
            return BrakeToggleResult(kind=BrakeToggleOutcomeKind.NOT_REGISTERED)
        self.engaged = False
        self.mode = None
        self.reason = None
        self.release_calls.append(released_by)
        return BrakeToggleResult(kind=BrakeToggleOutcomeKind.RELEASED)


class FakeBrakeConfirmationStore:
    """`BrakeConfirmationStorePort` en memoria: `consume` es de un solo uso,
    mismo contrato que `SqlBrakeConfirmationStore`."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[int, PendingBrakeAction, datetime]] = {}
        self._consumed: set[str] = set()

    async def create(
        self, *, nonce: str, chat_id: int, pending_action: PendingBrakeAction, expires_at: datetime
    ) -> None:
        self._rows[nonce] = (chat_id, pending_action, expires_at)

    async def consume(
        self, *, nonce: str, chat_id: int, now: datetime
    ) -> BrakeConfirmationRecord | None:
        if nonce in self._consumed:
            return None
        row = self._rows.get(nonce)
        if row is None:
            return None
        row_chat_id, pending_action, expires_at = row
        if row_chat_id != chat_id or expires_at <= now:
            return None
        self._consumed.add(nonce)
        return BrakeConfirmationRecord(
            nonce=nonce, chat_id=chat_id, pending_action=pending_action
        )
