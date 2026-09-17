"""Adaptadores SQL de `notifications/application/ports.py` (integracion).

`SqlSignalsForTicker` es la unica pieza que toca datos de `signals` desde
aqui: `notifications` no importa ese contexto (plan.md §4), asi que la
consulta cruzada vive en el lado que declara el puerto, sobre SQL crudo
-- nunca `from safent_ads.signals import ...`.

Simplificacion documentada: sin una columna que marque "ya ticado" en
`signals` (fuera del alcance de esta lane), "accionable" se define como
`kind IN (BUY, SELL, EXIT)` emitida en las ultimas 24h, top 20 por dinero
en juego. La deduplicacion real de envio la sigue garantizando
`notifications` (`dedupe_key` UNIQUE, NFR-6) -- esto solo decide que
candidatas entran al ticker, nunca si se reenvian."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.notifications.application.dto import Money as TickerMoney
from safent_ads.notifications.application.dto import SignalKind, TickerSignal
from safent_ads.notifications.application.ports import (
    BrakeConfirmationRecord,
    TelegramCallbackRecord,
)
from safent_ads.notifications.domain.brake_confirmation import PendingBrakeAction
from safent_ads.notifications.domain.callback import CallbackAction
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.value_objects import (
    ActiveHoursWindow,
    DeliveryState,
    DigestSchedule,
    Severity,
)
from safent_ads.shared.ids import BusinessId

_ACTIONABLE_LOOKBACK = timedelta(hours=24)
_ACTIONABLE_LIMIT = 20
_MINOR_UNITS_PER_MAJOR = 100
_TICKER_SIGNAL_KINDS = ("BUY", "SELL", "EXIT")

_SELECT_ACTIONABLE_SIGNALS = text("""
    SELECT s.entity_ref, s.kind, s.strength, s.cause, s.data_window,
           s.money_at_stake_minor, e.name AS entity_name, e.platform
      FROM signals s
      JOIN ad_entities e ON e.entity_ref = s.entity_ref
     WHERE s.business_id = :business_id
       AND s.kind = ANY(:kinds)
       AND s.emitted_at >= :since
     ORDER BY s.money_at_stake_minor DESC
     LIMIT :limit
""")

_RESERVE_NOTIFICATION = text("""
    INSERT INTO notifications
        (id, business_id, channel, severity, kind, payload, dedupe_key,
         delivery_state, attempt_count, message_id, sent_at)
    VALUES
        (:id, :business_id, 'telegram', :severity, :kind, :payload, :dedupe_key,
         'PENDING', 0, NULL, NULL)
    ON CONFLICT (dedupe_key) DO NOTHING
    RETURNING id
""")

_UPSERT_NOTIFICATION = text("""
    INSERT INTO notifications
        (id, business_id, channel, severity, kind, payload, dedupe_key,
         delivery_state, attempt_count, message_id, sent_at)
    VALUES
        (:id, :business_id, 'telegram', :severity, :kind, :payload, :dedupe_key,
         :delivery_state, :attempt_count, :message_id, :sent_at)
    ON CONFLICT (dedupe_key) DO UPDATE SET
        delivery_state = EXCLUDED.delivery_state,
        attempt_count = EXCLUDED.attempt_count,
        message_id = EXCLUDED.message_id,
        sent_at = EXCLUDED.sent_at,
        updated_at = now()
""")

_INSERT_DIGEST_QUEUE_ENTRY = text("""
    INSERT INTO notifications
        (id, business_id, channel, severity, kind, payload, dedupe_key, delivery_state)
    VALUES
        (:id, :business_id, 'telegram', 'DIGEST', 'digest', :payload, :dedupe_key, 'PENDING')
""")

_SELECT_PENDING_DIGEST_ENTRIES = text("""
    SELECT id, payload
      FROM notifications
     WHERE business_id = :business_id AND kind = 'digest' AND delivery_state = 'PENDING'
     ORDER BY created_at
""")

_MARK_DIGEST_ENTRIES_SUPPRESSED = text("""
    UPDATE notifications SET delivery_state = 'SUPPRESSED', updated_at = now()
     WHERE id = ANY(:ids)
""")

# 0023_owner_settings: hora del digest diario por negocio (PUT /settings).
# `digest_hour` a NULL = el negocio nunca lo personalizo, usar el valor por
# defecto que trae `SqlPendingDigest` (`ADS_DIGEST_HOUR`,
# `composition/settings.py`) interpretado en `ADS_TZ`, nunca en la zona del
# negocio -- mismo criterio que ya aplicaba esta consulta para el horario
# activo antes de esta rama.
_SELECT_BUSINESS_DIGEST_HOUR = text("""
    SELECT digest_hour, timezone FROM businesses WHERE id = :business_id
""")


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _severity_column(notification: Notification) -> str:
    if notification.kind.value == "digest":
        return "DIGEST"
    if notification.severity is Severity.CRITICAL:
        return "CRITICAL"
    return "NORMAL"


class SqlNotificationOutbox:
    """`NotificationOutboxPort` sobre `notifications` (0010_notifications.py,
    `dedupe_key` UNIQUE = NFR-6). `try_reserve` inserta PENDING antes de
    llamar al mensajero. Un crash tras la entrega puede dejar resultado
    incierto, pero una vuelta posterior no vuelve a enviar a ciegas."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_reserve(self, notification: Notification) -> bool:
        result = await self._session.execute(
            _RESERVE_NOTIFICATION, _notification_params(notification)
        )
        reserved = result.first() is not None
        await self._session.commit()
        return reserved

    async def save(self, notification: Notification) -> None:
        is_sent = notification.delivery_state is DeliveryState.SENT
        await self._session.execute(
            _UPSERT_NOTIFICATION,
            {
                **_notification_params(notification),
                "delivery_state": notification.delivery_state.value.upper(),
                "attempt_count": notification.delivery_attempts,
                "message_id": notification.platform_message_id,
                "sent_at": _now_utc() if is_sent else None,
            },
        )
        await self._session.commit()


def _notification_params(notification: Notification) -> dict[str, Any]:
    return {
        "id": notification.notification_id,
        "business_id": notification.business_id.value,
        "severity": _severity_column(notification),
        "kind": notification.kind.value,
        "payload": _payload_json(notification.body),
        "dedupe_key": notification.dedupe_key.value,
    }


class SqlPendingDigest:
    """`PendingDigestPort` sobre `notifications` (`kind='digest'`,
    `delivery_state='PENDING'` como cola; `pop_due` la vacia marcando
    `SUPPRESSED` -- no `SENT`, porque el digest real que se entrega al
    propietario es un unico mensaje agregado con su propio
    `dedupe_key`/fila, construido por quien llama `pop_due`
    (`PublishDigest`, `notifications/application`).

    `pop_due` libera la cola una vez al dia, a la hora exacta de
    `businesses.digest_hour` (0023_owner_settings) -- ya no "en la primera
    hora activa" (ver docstring de la migracion: "todavia sin consumidor
    propio"). Si el propietario nunca lo personalizo via `PUT /settings`
    (`digest_hour IS NULL`), se usa `digest_hour_default`
    (`ADS_DIGEST_HOUR`) interpretado en la zona de `active_hours`
    (`ADS_TZ`) -- mismo criterio de "todo o nada" que ya aplicaba esta
    clase para `active_hours_start`/`active_hours_end`: solo la zona del
    NEGOCIO gana cuando el propio negocio fijo su hora."""

    def __init__(
        self, session: AsyncSession, *, active_hours: ActiveHoursWindow, digest_hour_default: int
    ) -> None:
        self._session = session
        self._default_tz = active_hours.tz
        self._digest_hour_default = digest_hour_default

    async def enqueue(
        self, business_id: BusinessId, signals: list[TickerSignal], *, queued_at: datetime
    ) -> None:
        entry_id = uuid.uuid4()
        await self._session.execute(
            _INSERT_DIGEST_QUEUE_ENTRY,
            {
                "id": entry_id,
                "business_id": business_id.value,
                "payload": _payload_json_signals(signals, queued_at),
                "dedupe_key": f"digest-queue:{business_id}:{entry_id}",
            },
        )
        await self._session.commit()

    async def pop_due(self, business_id: BusinessId, *, at: datetime) -> list[TickerSignal]:
        schedule = await self._resolve_digest_schedule(business_id)
        if not schedule.is_due(at):
            return []
        rows = (
            await self._session.execute(
                _SELECT_PENDING_DIGEST_ENTRIES, {"business_id": business_id.value}
            )
        ).mappings().all()
        if not rows:
            return []
        signals = [signal for row in rows for signal in _parse_payload_signals(row["payload"])]
        await self._session.execute(
            _MARK_DIGEST_ENTRIES_SUPPRESSED, {"ids": [row["id"] for row in rows]}
        )
        await self._session.commit()
        return signals

    async def _resolve_digest_schedule(self, business_id: BusinessId) -> DigestSchedule:
        row = (
            await self._session.execute(
                _SELECT_BUSINESS_DIGEST_HOUR, {"business_id": business_id.value}
            )
        ).mappings().one_or_none()
        if row is None or row["digest_hour"] is None:
            return DigestSchedule(hour=self._digest_hour_default, tz=self._default_tz)
        return DigestSchedule(hour=int(row["digest_hour"]), tz=ZoneInfo(str(row["timezone"])))


class SqlSignalsForTicker:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_actionable_signals(self, business_id: BusinessId) -> list[TickerSignal]:
        rows = (
            await self._session.execute(
                _SELECT_ACTIONABLE_SIGNALS,
                {
                    "business_id": business_id.value,
                    "kinds": list(_TICKER_SIGNAL_KINDS),
                    "since": _now_utc() - _ACTIONABLE_LOOKBACK,
                    "limit": _ACTIONABLE_LIMIT,
                },
            )
        ).mappings()
        return [_to_ticker_signal(row) for row in rows]


def _to_ticker_signal(row: RowMapping) -> TickerSignal:
    return TickerSignal(
        entity_name=row["entity_name"],
        platform_label=row["platform"],
        kind=SignalKind(str(row["kind"]).lower()),
        strength=int(row["strength"]),
        cause_text=row["cause"],
        window_label=row["data_window"],
        money_at_stake=TickerMoney(
            Decimal(int(row["money_at_stake_minor"])) / _MINOR_UNITS_PER_MAJOR
        ),
    )


def _payload_json(body: str) -> str:
    return json.dumps({"body": body})


def _payload_json_signals(signals: list[TickerSignal], queued_at: datetime) -> str:
    return json.dumps(
        {
            "queued_at": queued_at.isoformat(),
            "signals": [
                {
                    "entity_name": signal.entity_name,
                    "platform_label": signal.platform_label,
                    "kind": signal.kind.value,
                    "strength": signal.strength,
                    "cause_text": signal.cause_text,
                    "window_label": signal.window_label,
                    "money_at_stake_amount": str(signal.money_at_stake.amount),
                    "money_at_stake_currency": signal.money_at_stake.currency,
                }
                for signal in signals
            ],
        }
    )


_INSERT_TELEGRAM_CALLBACK = text("""
    INSERT INTO telegram_callbacks
        (nonce, proposal_id, chat_id, message_id, diff_hash, action, expires_at)
    VALUES (:nonce, :proposal_id, :chat_id, :message_id, :diff_hash, :action, :expires_at)
""")

_CONSUME_TELEGRAM_CALLBACK = text("""
    UPDATE telegram_callbacks SET consumed_at = :now
     WHERE nonce = :nonce AND chat_id = :chat_id AND message_id = :message_id
       AND consumed_at IS NULL AND expires_at > :now
    RETURNING proposal_id, chat_id, message_id, diff_hash, action
""")

_PEEK_TELEGRAM_CALLBACK = text("""
    SELECT proposal_id, chat_id, message_id, diff_hash, action
      FROM telegram_callbacks
     WHERE nonce = :nonce AND chat_id = :chat_id AND message_id = :message_id
       AND expires_at > :now
""")

_FIND_TELEGRAM_CALLBACK_BY_NONCE = text("""
    SELECT proposal_id, chat_id, message_id, diff_hash, action
      FROM telegram_callbacks WHERE nonce = :nonce
""")


class SqlTelegramCallbackStore:
    """`TelegramCallbackStorePort` sobre `telegram_callbacks`
    (0010_notifications). `consume` es la unica via de un solo uso real:
    `UPDATE ... WHERE consumed_at IS NULL` es atomica bajo concurrencia (dos
    pulsaciones a la vez sobre el mismo nonce, a lo sumo una gana la fila);
    el trigger `telegram_callbacks_single_use` de la migracion es la ultima
    linea de defensa si algun camino se saltase ese `WHERE`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

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
        await self._session.execute(
            _INSERT_TELEGRAM_CALLBACK,
            {
                "nonce": nonce,
                "proposal_id": proposal_id,
                "chat_id": chat_id,
                "message_id": message_id,
                "diff_hash": diff_hash,
                "action": action.value,
                "expires_at": expires_at,
            },
        )

    async def consume(
        self, *, nonce: str, chat_id: int, message_id: int, now: datetime
    ) -> TelegramCallbackRecord | None:
        result = await self._session.execute(
            _CONSUME_TELEGRAM_CALLBACK,
            {"nonce": nonce, "chat_id": chat_id, "message_id": message_id, "now": now},
        )
        row = result.one_or_none()
        return None if row is None else _row_to_callback_record(nonce, row)

    async def peek(
        self, *, nonce: str, chat_id: int, message_id: int, now: datetime
    ) -> TelegramCallbackRecord | None:
        result = await self._session.execute(
            _PEEK_TELEGRAM_CALLBACK,
            {"nonce": nonce, "chat_id": chat_id, "message_id": message_id, "now": now},
        )
        row = result.one_or_none()
        return None if row is None else _row_to_callback_record(nonce, row)

    async def find_by_nonce(self, nonce: str) -> TelegramCallbackRecord | None:
        result = await self._session.execute(_FIND_TELEGRAM_CALLBACK_BY_NONCE, {"nonce": nonce})
        row = result.one_or_none()
        return None if row is None else _row_to_callback_record(nonce, row)


def _row_to_callback_record(nonce: str, row: Any) -> TelegramCallbackRecord:  # noqa: ANN401
    return TelegramCallbackRecord(
        nonce=nonce,
        proposal_id=str(row.proposal_id),
        chat_id=row.chat_id,
        message_id=row.message_id,
        diff_hash=row.diff_hash,
        action=CallbackAction(row.action),
    )


_SELECT_PENDING_PROPOSAL_IDS = text("""
    SELECT id FROM proposals WHERE state = 'pending' ORDER BY created_at ASC LIMIT :limit
""")


class SqlPendingProposalIds:
    """`PendingProposalIdsPort` (este branch, `/pendientes`). Solo ids: el
    contenido de cada tarjeta lo resuelve `ApprovalGatewayPort`, que ya
    existe -- esta consulta es deliberadamente la unica pieza nueva."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_pending_ids(self, *, limit: int) -> tuple[str, ...]:
        rows = await self._session.execute(_SELECT_PENDING_PROPOSAL_IDS, {"limit": limit})
        return tuple(str(row.id) for row in rows)


# Un solo segundo toque vivo por chat: pedir `/freno` de nuevo invalida el
# nonce anterior (no consumido). Acota la acumulacion de nonces sin limite
# (revision F4 §2) sin necesitar reloj: la fila sin consumir simplemente
# deja de existir.
_DELETE_LIVE_BRAKE_CONFIRMATIONS = text("""
    DELETE FROM telegram_brake_confirmations
    WHERE chat_id = :chat_id AND consumed_at IS NULL
""")

_INSERT_BRAKE_CONFIRMATION = text("""
    INSERT INTO telegram_brake_confirmations (nonce, chat_id, pending_action, expires_at)
    VALUES (:nonce, :chat_id, :pending_action, :expires_at)
""")

_CONSUME_BRAKE_CONFIRMATION = text("""
    UPDATE telegram_brake_confirmations SET consumed_at = :now
     WHERE nonce = :nonce AND chat_id = :chat_id
       AND consumed_at IS NULL AND expires_at > :now
    RETURNING pending_action
""")


class SqlBrakeConfirmationStore:
    """`BrakeConfirmationStorePort` sobre `telegram_brake_confirmations`
    (0024_brake_confirmations). `consume` es la unica via de un solo uso,
    igual que `SqlTelegramCallbackStore.consume` -- `UPDATE ... WHERE
    consumed_at IS NULL` es atomica bajo concurrencia."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, nonce: str, chat_id: int, pending_action: PendingBrakeAction, expires_at: datetime
    ) -> None:
        await self._session.execute(_DELETE_LIVE_BRAKE_CONFIRMATIONS, {"chat_id": chat_id})
        await self._session.execute(
            _INSERT_BRAKE_CONFIRMATION,
            {
                "nonce": nonce,
                "chat_id": chat_id,
                "pending_action": pending_action.value,
                "expires_at": expires_at,
            },
        )

    async def consume(
        self, *, nonce: str, chat_id: int, now: datetime
    ) -> BrakeConfirmationRecord | None:
        result = await self._session.execute(
            _CONSUME_BRAKE_CONFIRMATION, {"nonce": nonce, "chat_id": chat_id, "now": now}
        )
        row = result.one_or_none()
        if row is None:
            return None
        return BrakeConfirmationRecord(
            nonce=nonce, chat_id=chat_id, pending_action=PendingBrakeAction(row.pending_action)
        )


def _parse_payload_signals(payload: Any) -> list[TickerSignal]:  # noqa: ANN401 - JSONB heterogeneo
    document = payload if isinstance(payload, dict) else json.loads(str(payload))
    return [
        TickerSignal(
            entity_name=item["entity_name"],
            platform_label=item["platform_label"],
            kind=SignalKind(item["kind"]),
            strength=item["strength"],
            cause_text=item["cause_text"],
            window_label=item["window_label"],
            money_at_stake=TickerMoney(
                Decimal(item["money_at_stake_amount"]), item["money_at_stake_currency"]
            ),
        )
        for item in document.get("signals", [])
    ]
