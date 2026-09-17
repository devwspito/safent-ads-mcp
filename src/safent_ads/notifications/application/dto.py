"""DTOs de lectura que entran a `notifications/application` desde otros
contextos (plan.md: "leidos por puertos, nunca importando el contexto
vecino"). `signals`/`accounts` construyen sus propios agregados; aqui solo
se declara la forma que `notifications` necesita consumir."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

_MAX_SIGNAL_STRENGTH = 100


class SignalKind(StrEnum):
    """Espejo local de `signals.domain.signal_engine.SignalKind`
    (plan.md §5). Duplicado a proposito: `notifications` no importa
    `signals` (grafo aciclico, plan.md §4); cada lado del puerto declara su
    propia forma y la integracion los concilia."""

    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class Money:
    """Cantidad monetaria minima para render (no es el `Money` de negocio
    completo: sin aritmetica, solo lo que el ticker necesita mostrar)."""

    amount: Decimal
    currency: str = "EUR"

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("Money.amount no puede ser negativo")


@dataclass(frozen=True, slots=True)
class TickerSignal:
    """Una fila del ticker (contracts/telegram.md §Formatos de mensaje):
    una senal ya evaluada, lista para render, ordenada por `money_at_stake`."""

    entity_name: str
    platform_label: str
    kind: SignalKind
    strength: int
    cause_text: str
    window_label: str
    money_at_stake: Money

    def __post_init__(self) -> None:
        if not 0 <= self.strength <= _MAX_SIGNAL_STRENGTH:
            raise ValueError(f"strength fuera de rango 0-100: {self.strength}")


@dataclass(frozen=True, slots=True)
class CriticalEvent:
    """Evento critico que interrumpe siempre (contracts/telegram.md §Critico):
    cuenta suspendida, freno activado, gasto 3x la media, cadena de
    auditoria rota, plataforma caida."""

    business_name: str
    title: str
    detail: str
    note: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalRequestView:
    """Una propuesta lista para pintarse como tarjeta de aprobacion nivel 1
    (contracts/telegram.md §Solicitud de aprobacion). `before_label`/
    `after_label`/`impact_label` llegan ya formateados por quien construye
    la vista (misma frontera que `TickerSignal`: el DTO es de presentacion,
    no reabre la aritmetica de `proposals`/`execution`)."""

    proposal_id: str
    diff_hash: str
    entity_name: str
    platform_label: str
    kind: SignalKind
    parameter_label: str
    before_label: str
    after_label: str
    change_note: str | None
    cause_text: str
    window_label: str
    impact_label: str
    rule_id: str
    expires_at: datetime
    is_spend_increase: bool

    def __post_init__(self) -> None:
        if not self.cause_text:
            raise ValueError("ApprovalRequestView.cause_text no puede estar vacio")


@dataclass(frozen=True, slots=True)
class CauseGroupApprovalView:
    """Lote por causa (contracts/telegram.md §Lote por causa): `[Aprobar las
    N]` genera N autorizaciones independientes, nunca una sola compuesta."""

    cause_text: str
    total_impact_label: str
    anchor_proposal_id: str
    anchor_diff_hash: str
    items: tuple[ApprovalRequestView, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("CauseGroupApprovalView.items no puede estar vacio")


@dataclass(frozen=True, slots=True)
class ConfirmSpendIncreaseView:
    """Segundo toque obligatorio para SUBIR (contracts/telegram.md): el
    primer toque nunca aprueba una propuesta que aumenta gasto."""

    entity_name: str
    before_label: str
    after_label: str
    impact_label: str
    monthly_cap_label: str


@dataclass(frozen=True, slots=True)
class ApprovalEvidenceLine:
    metric: str
    actual: float
    target: float
    window_label: str


@dataclass(frozen=True, slots=True)
class ApprovalDetailView:
    """Nivel 2 de la tarjeta de aprobacion (contracts/telegram.md
    `[Detalle]`): evidencia metrica y guardarraíles aplicables. "Tope
    mensual restante" se simplifica al tope compuesto del ambito (Assumption
    documentada: el ledger de gasto exacto vive en `execution`, fuera del
    alcance de esta lane) y "estado de aprendizaje" queda fuera (depende de
    `accounts.AdEntity`, otra lane)."""

    evidence: tuple[ApprovalEvidenceLine, ...]
    guardrail_floor_label: str
    guardrail_ceiling_label: str
    guardrail_monthly_cap_label: str


@dataclass(frozen=True, slots=True)
class AutoReceiptEvent:
    """Recibo de una accion autonoma ya ejecutada (contracts/telegram.md
    §Recibo de accion autonoma): solo lectura + deshacer."""

    business_name: str
    entity_name: str
    platform_label: str
    parameter_label: str
    before_label: str
    after_label: str
    change_note: str | None
    cause_text: str
    rule_id: str
    guardrail_note: str
    change_ordinal: int
    change_max: int
    decided_at: datetime
    undo_deadline: datetime
    proposal_id: str
    diff_hash: str


@dataclass(frozen=True, slots=True)
class CredentialHealthAlertEvent:
    """Una credencial de plataforma paso a un estado que exige reconectar
    (threat-model.md C-21, tasks.md T126). `account_ref`/`health_code` son
    identificadores estables (para deduplicar); `account_label`/
    `health_label` ya vienen en espanol, listos para el cuerpo del mensaje
    -- igual que `platform_label` en `TickerSignal`, esta lane no vuelve a
    traducir un vocabulario que no es el suyo (`accounts.domain`)."""

    business_name: str
    account_ref: str
    account_label: str
    health_code: str
    health_label: str
    error_code: str | None
    occurred_at: datetime
