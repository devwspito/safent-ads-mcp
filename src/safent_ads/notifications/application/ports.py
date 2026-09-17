"""Puertos de `notifications/application` (plan.md §5: "Puerto:
MessengerPort"). Implementaciones reales en `infrastructure/` (esta lane) o
en la integracion posterior; los tests de caso de uso usan los dobles de
`notifications/testing/`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from safent_ads.notifications.application.dto import (
    ApprovalDetailView,
    Money,
    SignalKind,
    TickerSignal,
)
from safent_ads.notifications.domain.brake_confirmation import PendingBrakeAction
from safent_ads.notifications.domain.callback import CallbackAction
from safent_ads.notifications.domain.notification import Notification
from safent_ads.notifications.domain.pairing import PairingCode, PairingStatus
from safent_ads.shared.ids import BusinessId


@dataclass(frozen=True, slots=True)
class KeyboardButton:
    """Un boton del teclado inline. `callback_data` ya viene codificado
    (`CallbackData.encode()`, ≤64 bytes) — este VO no repite esa validacion."""

    label: str
    callback_data: str


# Filas de botones. `()` (vacio, distinto de `None`) es la senal explicita
# de "quitar el teclado" (contracts/telegram.md regla 4: "reply_markup
# vacío"); `None` significa "no tocar el teclado que ya tenga el mensaje" —
# la API de Telegram distingue exactamente estos dos casos por si el campo
# se omite o se manda vacio, y `[Detalle]` (regla: "no consume el nonce")
# se apoya en ese "no tocar" para no tener que reconstruir los otros botones.
InlineKeyboard = tuple[tuple[KeyboardButton, ...], ...]


class MessengerPort(Protocol):
    """Puerto de entrega. `text` es texto plano ya compuesto por el caso de
    uso; el adaptador de Telegram (T041) es quien decide como escaparlo y
    con que `parse_mode` enviarlo — la capa de aplicacion no conoce HTML."""

    async def send(
        self,
        *,
        chat_id: int,
        text: str,
        disable_notification: bool = False,
        reply_markup: InlineKeyboard | None = None,
    ) -> int:
        """Devuelve el `message_id` de la plataforma."""
        ...

    async def edit(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboard | None = None,
    ) -> None: ...


class SignalsForTickerPort(Protocol):
    """Puerto de lectura hacia `signals` (leido, nunca importado: plan.md
    §4). La implementacion real la cablea la integracion; hasta entonces
    `notifications/testing/fakes.py` la sustituye en los tests."""

    async def list_actionable_signals(self, business_id: BusinessId) -> list[TickerSignal]: ...


class NotificationOutboxPort(Protocol):
    """Persistencia de la deduplicacion (NFR-6). `try_reserve` es la version
    con efectos del `DuplicateSendGuard` del dominio: la tabla real
    (`notifications`, UNIQUE `dedupe_key`, migracion fuera de esta lane) es
    quien hace cumplir el invariante en produccion."""

    async def try_reserve(self, notification: Notification) -> bool:
        """Persist PENDING atomically before any external delivery."""
        ...

    async def save(self, notification: Notification) -> None: ...


class PendingDigestPort(Protocol):
    """Cola de senales pendientes de digest mientras estamos fuera del
    horario activo (contracts/telegram.md §Digest)."""

    async def enqueue(
        self, business_id: BusinessId, signals: list[TickerSignal], *, queued_at: datetime
    ) -> None: ...

    async def pop_due(self, business_id: BusinessId, *, at: datetime) -> list[TickerSignal]:
        """Devuelve y vacia la cola si `at` cae dentro del horario activo;
        lista vacia si no hay nada pendiente o si `at` sigue fuera de horario."""
        ...


@dataclass(frozen=True, slots=True)
class CallbackOutcome:
    """Resultado de resolver una pulsacion de callback
    (contracts/telegram.md §`callback_data`). `edited_text`, si no es
    `None`, reemplaza el mensaje original (regla 4: "resuelta la decision:
    `edit_message_text`... el mensaje queda congelado"). `reply_markup`
    sigue la misma semantica tri-estado que `MessengerPort.edit`."""

    alert_text: str
    show_alert: bool
    edited_text: str | None = None
    reply_markup: InlineKeyboard | None = None


class CallbackResolverPort(Protocol):
    """Puerto que resuelve una pulsacion de boton ya autenticada (allow-list
    verificada por el adaptador). `message_id` liga la pulsacion al mensaje
    exacto que la origino — el nonce esta atado a `(proposal_id, chat_id,
    message_id, diff_hash, action)` (contracts/telegram.md §`callback_data`).
    Hasta que US3 cablea la resolucion real, el adaptador usa un resolutor
    que rechaza toda pulsacion como caducada (regla 1), nunca con
    auto-aprobacion."""

    async def resolve(
        self, *, callback_data: str, chat_id: int, from_user_id: int, message_id: int
    ) -> CallbackOutcome: ...


# ---------------------------------------------------------------------------
# Nonces de callback (`telegram_callbacks`, 0010_notifications) y el unico
# camino de decision humana (contracts/telegram.md: "Telegram debe llamar
# al MISMO caso de uso, nunca uno paralelo"). Ambos puertos viven aqui,
# pero solo los implementa infraestructura: la app (`ResolveCallback`)
# nunca importa `proposals`/`execution` directamente.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TelegramCallbackRecord:
    """Fila viva de `telegram_callbacks` (contracts/telegram.md
    §`callback_data`)."""

    nonce: str
    proposal_id: str
    chat_id: int
    message_id: int
    diff_hash: str
    action: CallbackAction


class TelegramCallbackStorePort(Protocol):
    """Persistencia de `CallbackNonce` (plan.md §5). `consume` es la unica
    via de un solo uso (`UPDATE ... WHERE consumed_at IS NULL`, T de
    0010_notifications); `peek` no marca nada consumido — solo la usa
    `[Detalle]`, que el contrato exige que no gaste el nonce de aprobacion."""

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
        """`nonce` lo genera el llamador (`domain.callback.generate_nonce`)
        ANTES de enviar el mensaje: el teclado con el `callback_data` tiene
        que viajar en el mismo `sendMessage` que crea el mensaje, pero
        `message_id` solo se conoce DESPUES de enviarlo -- por eso el nonce
        nace fuera de este puerto y esta llamada solo lo persiste una vez
        que ya se conoce `message_id`."""
        ...

    async def consume(
        self, *, nonce: str, chat_id: int, message_id: int, now: datetime
    ) -> TelegramCallbackRecord | None: ...

    async def peek(
        self, *, nonce: str, chat_id: int, message_id: int, now: datetime
    ) -> TelegramCallbackRecord | None: ...

    async def find_by_nonce(self, nonce: str) -> TelegramCallbackRecord | None:
        """Localiza la fila exista o no exista viva -- unico dato disponible
        para reeditar "el estado real" (regla 1) cuando el nonce ya caduco
        o se consumio, sin el cual no habria forma de saber a que propuesta
        se referia la pulsacion."""
        ...


class DecisionKind(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    POSTPONED = "postponed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class DecisionResult:
    kind: DecisionKind
    denial_reason: str | None = None


class UndoResultKind(StrEnum):
    CANCELLED = "cancelled"
    RESTORED = "restored"
    COMPENSATING_PROPOSAL_CREATED = "compensating_proposal_created"
    NOT_ALLOWED = "not_allowed"
    # FR-15, idempotencia: `UndoExecution` ya deshizo este intento antes --
    # distinto de `NOT_ALLOWED` (nunca hubo nada que deshacer) para que el
    # recibo de Telegram diga la verdad ("ya se deshizo", no "no disponible").
    ALREADY_UNDONE = "already_undone"


@dataclass(frozen=True, slots=True)
class UndoResult:
    kind: UndoResultKind
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class LiveProposalView:
    """Snapshot de una `Proposal` viva, ya lista para pintarse
    (contracts/telegram.md): lo minimo que `ResolveCallback` necesita para
    decidir y para reeditar una tarjeta, sin que `notifications/application`
    importe `proposals.domain`."""

    proposal_id: str
    diff_hash: str
    state_label: str
    is_pending: bool
    entity_name: str
    platform_label: str
    kind: SignalKind
    parameter_label: str
    before_label: str
    after_label: str
    change_note: str | None
    cause_text: str
    rule_id: str
    window_label: str
    impact_label: str
    expires_at: datetime
    is_spend_increase: bool


class ApprovalGatewayPort(Protocol):
    """Unico camino de decision humana (contracts/telegram.md: "Telegram
    debe llamar al MISMO caso de uso que REST, nunca uno paralelo"). La
    implementacion real (`notifications/infrastructure/
    proposal_approval_gateway.py`) envuelve
    `proposals.application.submit_approval.SubmitApproval` -- el mismo
    objeto que usa `composition/execution_rest.py`."""

    async def get_live_proposal(self, proposal_id: str) -> LiveProposalView | None: ...

    async def get_group_members(self, anchor_proposal_id: str) -> tuple[LiveProposalView, ...]:
        """Otras propuestas `PENDING` con la misma `cause_key` que el ancla
        del lote (contracts/telegram.md §Lote por causa), ancla incluida."""
        ...

    async def get_detail(self, proposal_id: str) -> ApprovalDetailView | None: ...

    async def approve(
        self, *, proposal_id: str, diff_hash: str, decided_by: str
    ) -> DecisionResult: ...

    async def reject(
        self, *, proposal_id: str, diff_hash: str, decided_by: str
    ) -> DecisionResult: ...

    async def postpone(
        self, *, proposal_id: str, decided_by: str, hours: int
    ) -> DecisionResult: ...

    async def undo(self, *, proposal_id: str, initiated_by: str) -> UndoResult: ...


# ---------------------------------------------------------------------------
# Emparejamiento Telegram<->propietario (FR-25, `telegram_owner_chats`,
# 0022_telegram_pairing). `TelegramPairingGuardPort` lo usa `ResolveCallback`
# (contracts/telegram.md: "sin fila verificada el canal avisa pero no
# decide"); los demas los usan los casos de uso de
# `notifications/application/telegram_pairing.py` y
# `confirm_telegram_pairing.py`.
# ---------------------------------------------------------------------------


class TelegramPairingGuardPort(Protocol):
    """Comprobacion previa a resolver cualquier `callback_query`: allow-list
    la verifica el adaptador (`TelegramOwnerAllowList`), emparejamiento vivo
    lo verifica esto. `record_unpaired_callback` deja constancia en
    `decision_log` sin tomar ninguna decision (nunca se llega a
    `ApprovalGatewayPort`). `record_unpaired_command_denied` es el mismo
    criterio para un comando/callback que no es de aprobacion de propuestas
    (p.ej. `/freno`, security-review-f4.md §2 "ResolveBrakeCallback no
    escribe en decision_log"): ademas del chat, deja constancia de QUE
    comando se denego."""

    async def is_chat_paired(self, chat_id: int) -> bool: ...

    async def record_unpaired_callback(self, chat_id: int) -> None: ...

    async def record_unpaired_command_denied(self, chat_id: int, *, command: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PairingSnapshot:
    """Vista de una fila de `telegram_owner_chats` para `GET
    /telegram/pairing`. `pairing_code` viaja en claro SOLO mientras el
    estado efectivo es `pending` (el repositorio lo descifra) -- fuera de
    esa ventana siempre es `None`, igual que en la base solo vive cifrado."""

    status: PairingStatus
    chat_id: int | None
    paired_at: datetime | None
    code_expires_at: datetime | None
    pairing_code: str | None
    last_test_at: datetime | None


class TelegramPairingRepositoryPort(Protocol):
    """Persistencia de `telegram_owner_chats` desde el lado del
    PROPIETARIO (panel): siempre resuelto por `owner_id`, nunca por
    `chat_id` -- ver `TelegramPairingConfirmationPort` para el otro
    sentido, el que usa el bot."""

    async def get(self, owner_id: uuid.UUID, *, now: datetime) -> PairingSnapshot | None: ...

    async def start_pairing(
        self, *, owner_id: uuid.UUID, code: PairingCode, expires_at: datetime
    ) -> None:
        """UPSERT a `pending` con un codigo nuevo: invalida cualquier
        codigo o emparejamiento previo de este propietario (un solo codigo
        vivo a la vez, contracts/telegram.md)."""
        ...

    async def unpair(self, owner_id: uuid.UUID) -> None:
        """Idempotente: vuelve a `unpaired` exista o no exista fila
        todavia (`DELETE /telegram/pairing` es un 204 en ambos casos)."""
        ...

    async def record_test_message_sent(self, owner_id: uuid.UUID, *, at: datetime) -> None: ...


@dataclass(frozen=True, slots=True)
class PendingPairingCandidate:
    """Fila `pending` viva (sin caducar) candidata a que un `/emparejar
    <codigo>` la resuelva. Solo `owner_id` + el hash: el bot no conoce
    ningun otro dato de cada candidata hasta que el codigo hace match."""

    owner_id: uuid.UUID
    pairing_code_hash: str


class TelegramPairingConfirmationPort(Protocol):
    """Solo lo usa el bot: `/emparejar <codigo>` llega con un `chat_id`
    autorizado (allow-list) pero sin `owner_id` -- hay que buscar por
    CODIGO entre las candidatas vivas, nunca al reves."""

    async def list_pending(self, *, now: datetime) -> list[PendingPairingCandidate]: ...

    async def confirm(self, *, owner_id: uuid.UUID, chat_id: int, at: datetime) -> None: ...


class TelegramPairingAttemptsPort(Protocol):
    """Limite de 3 intentos de `/emparejar` por chat y hora
    (contracts/telegram.md), mismo patron que `login_attempts` (iam)."""

    async def count_recent(self, chat_id: int, *, since: datetime) -> int: ...

    async def record(self, chat_id: int, *, succeeded: bool, at: datetime) -> None: ...


class TelegramPairingConfirmationOutcome(StrEnum):
    """Desenlace de `/emparejar <codigo>` (contracts/telegram.md)."""

    CONFIRMED = "confirmed"
    INVALID_CODE = "invalid_code"
    NO_MATCH = "no_match"
    RATE_LIMITED = "rate_limited"


class TelegramPairingCommandPort(Protocol):
    """Puerto que resuelve `/emparejar <codigo>` ya autenticado por la
    allow-list (verificada por el adaptador, mismo criterio que
    `CallbackResolverPort`)."""

    async def confirm(
        self, *, chat_id: int, code_text: str
    ) -> TelegramPairingConfirmationOutcome: ...


@dataclass(frozen=True, slots=True)
class PendingTestMessage:
    """Tarea de entrega de `POST /telegram/pairing/test-message`
    (`telegram_test_messages`, 0021): ya resuelta a su `chat_id` -- solo
    `ads-worker` tiene el `MessengerPort` real (un solo `Bot` por proceso,
    contracts/telegram.md §1)."""

    notification_id: uuid.UUID
    chat_id: int
    body: str


class TestMessageOutboxPort(Protocol):
    """Cola de mensajes de prueba de emparejamiento (FR-25): `ads-api` los
    encola (`enqueue`, nunca habla con Telegram), `ads-worker` los drena
    (`claim_pending`) y confirma el desenlace. `count_recent` es el freno
    de `POST /telegram/pairing/test-message` (security-review-f4.md item 1),
    mismo patron de conteo-en-ventana que `TelegramPairingAttemptsPort`."""

    async def enqueue(
        self,
        *,
        notification_id: uuid.UUID,
        owner_id: uuid.UUID,
        chat_id: int,
        body: str,
        at: datetime,
    ) -> None: ...

    async def count_recent(self, owner_id: uuid.UUID, *, since: datetime) -> int: ...

    async def claim_pending(self, *, limit: int) -> list[PendingTestMessage]: ...

    async def mark_sent(self, notification_id: uuid.UUID, *, platform_message_id: int) -> None: ...

    async def mark_failed(self, notification_id: uuid.UUID) -> None: ...


# ---------------------------------------------------------------------------
# `/estado` (este branch): un resumen por negocio, reusando lo que ya lee
# `panel`/`rules` a este mismo nivel (N7/N4) sin importar `panel` (plan.md
# §4: "ambos leen, nunca importan" -- `notifications` repite su propia
# consulta minima en vez de importar ese contexto hermano).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BusinessSummaryView:
    business_id: str
    name: str


@dataclass(frozen=True, slots=True)
class BusinessStatusView:
    """Lo que `/estado` pinta de un negocio: gasto de hoy frente a topes,
    ritmo, senales accionables abiertas, propuestas pendientes, freno y
    frescura de datos."""

    spend_today: Money
    daily_cap: Money | None
    pacing_index_pct: float
    pacing_projection_pct: float
    open_signals: int
    pending_proposals: int
    brake_engaged: bool
    brake_mode: str | None
    freshness_lag_minutes: int
    freshness_is_stale: bool
    is_partial: bool


class BusinessStatusPort(Protocol):
    """Puerto de lectura de `/estado` (un unico resumen agregado, sin
    desglose por entidad -- eso ya lo tiene el panel)."""

    async def list_businesses(self) -> tuple[BusinessSummaryView, ...]: ...

    async def get_status(self, business_id: BusinessId) -> BusinessStatusView: ...


class TelegramStatusCommandPort(Protocol):
    """Resuelve `/estado` ya autenticado por la allow-list (verificada por
    el adaptador). Devuelve un texto por negocio -- el adaptador es quien
    los envia, uno por mensaje."""

    async def execute(self, *, chat_id: int) -> tuple[str, ...]: ...


class TelegramPendingCommandPort(Protocol):
    """Resuelve `/pendientes` ya autenticado por la allow-list. Devuelve
    cuantas tarjetas se enviaron -- el envio en si ya ocurrio dentro del
    resolutor (necesita el mismo `MessengerPort` para atar cada nonce a su
    `message_id`, ver `ListPendingProposals`)."""

    async def execute(self, *, chat_id: int) -> int: ...


# ---------------------------------------------------------------------------
# `/pendientes`: mismas tarjetas y reglas de `callback_data` que
# `RequestApproval` (contracts/telegram.md), sobre una foto nueva del
# estado -- nunca reenvia un nonce ya emitido.
# ---------------------------------------------------------------------------


class PendingProposalIdsPort(Protocol):
    """`proposals.id` en estado `pending`, mas antiguas primero
    (contracts/telegram.md: "/pendientes... 10 mas antiguas primero" segun
    esta rama). Solo ids: el contenido para pintar la tarjeta lo resuelve
    `ApprovalGatewayPort.get_live_proposal`, ya existente."""

    async def list_pending_ids(self, *, limit: int) -> tuple[str, ...]: ...


# ---------------------------------------------------------------------------
# `/freno on|off` (contracts/telegram.md: "requiere segundo toque de
# confirmacion... escribe EmergencyBrakeEngaged"). El freno de Telegram es
# SIEMPRE de ambito global (mismo alcance por defecto que
# `composition/execution_rest.py::_parse_brake_scope`): el bot no ofrece
# elegir negocio/cuenta, solo el interruptor general.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrakeStatusView:
    engaged: bool
    mode: str | None
    reason: str | None
    since: datetime | None


class BrakeToggleOutcomeKind(StrEnum):
    ENGAGED = "engaged"
    RELEASED = "released"
    ALREADY_ENGAGED = "already_engaged"
    NOT_REGISTERED = "not_registered"


@dataclass(frozen=True, slots=True)
class BrakeToggleResult:
    kind: BrakeToggleOutcomeKind
    status: BrakeStatusView | None = None


class BrakeGatewayPort(Protocol):
    """Unico camino de escritura del freno desde Telegram
    (contracts/telegram.md: "el mismo caso de uso que REST, nunca uno
    paralelo"). La implementacion real
    (`notifications/infrastructure/brake_gateway.py`) envuelve
    `execution.application.toggle_emergency_brake.ToggleEmergencyBrake` --
    el mismo objeto que usa `composition/execution_rest.py::post_kill_switch`."""

    async def get_status(self) -> BrakeStatusView: ...

    async def engage(self, *, reason: str, engaged_by: str) -> BrakeToggleResult: ...

    async def release(self, *, released_by: str) -> BrakeToggleResult: ...


@dataclass(frozen=True, slots=True)
class BrakeConfirmationRecord:
    """Fila viva de `telegram_brake_confirmations` (0024_brake_confirmations)."""

    nonce: str
    chat_id: int
    pending_action: PendingBrakeAction


class BrakeConfirmationStorePort(Protocol):
    """Persistencia del nonce de segundo toque de `/freno on|off`. `create`
    nace ANTES de enviar el mensaje (a diferencia de
    `TelegramCallbackStorePort`): el freno no ata el nonce a un
    `message_id` -- no hay una propuesta/tarjeta previa que editar, el
    primer toque siempre es un mensaje nuevo (ver docstring de
    `notifications/domain/brake_confirmation.py`)."""

    async def create(
        self, *, nonce: str, chat_id: int, pending_action: PendingBrakeAction, expires_at: datetime
    ) -> None: ...

    async def consume(
        self, *, nonce: str, chat_id: int, now: datetime
    ) -> BrakeConfirmationRecord | None: ...


@dataclass(frozen=True, slots=True)
class BrakeCommandReply:
    """Respuesta a `/freno`, `/freno on` o `/freno off`: texto mas, si
    aplica, el teclado de confirmacion del segundo toque."""

    text: str
    keyboard: InlineKeyboard | None = None


class TelegramBrakeCommandPort(Protocol):
    """Resuelve `/freno`, `/freno on` y `/freno off` ya autenticado por la
    allow-list (verificada por el adaptador, mismo criterio que
    `TelegramPairingCommandPort`). Ninguno de los tres aplica el freno por
    si solo -- `on`/`off` solo emiten el primer toque; `BrakeCallbackResolverPort`
    resuelve el segundo."""

    async def status(self, *, chat_id: int) -> BrakeCommandReply: ...

    async def request_engage(self, *, chat_id: int) -> BrakeCommandReply: ...

    async def request_release(self, *, chat_id: int) -> BrakeCommandReply: ...


class BrakeCallbackResolverPort(Protocol):
    """Resuelve el segundo toque (`brk:<nonce>:<y|n>`) ya autenticado por
    la allow-list -- mismo criterio que `CallbackResolverPort`, pero para
    el freno en vez de una propuesta."""

    async def resolve(
        self, *, callback_data: str, chat_id: int, from_user_id: int, message_id: int
    ) -> CallbackOutcome: ...
