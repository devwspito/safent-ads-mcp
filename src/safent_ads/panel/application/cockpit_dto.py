"""DTOs de lectura del cuadro de mando (026, tasks.md T006, contracts/
cockpit-read-model.md §2/§3). `panel` sigue sin importar `signals`/
`accounts`/`proposals`/`execution` en este modulo (plan.md §4): formas
propias, declaradas aqui; `sql_cockpit_read_model.py` (T007) rellena desde
los contextos reales.

`RowAction` diverge del contrato original en tres puntos, acordados con el
carril del panel (checklists de la 026, no re-derivar sin avisar):

1. Lleva `proposal_id`/`diff_hash` de la propuesta viva que sustenta la
   accion -- `POST /proposals/{id}/approve` los exige y el panel nunca
   fabrica uno. Sin propuesta que sustente la fila, `kind=NONE` con
   `reason`: nunca se anuncia una accion que el panel no puede ejecutar.
2. `friction` es un unico enum ordinal (`none < confirm`);
   la expansion de evidencia es el booleano ortogonal `requires_evidence`
   (`True` solo para `approve_increase`), no un cuarto valor de friccion.
3. `target` es `None` cuando `kind=NONE`: no hay ruta que ofrecer para una
   fila sin accion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from safent_ads.panel.application.dto import DegradedAccount, LearningState
from safent_ads.shared.read_models.dto import (
    Caps,
    Freshness,
    Measure,
    Money,
    Pacing,
    SpendBreakdown,
)


class CockpitWindow(StrEnum):
    TODAY = "today"
    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"


class RoiBasis(StrEnum):
    CONTRIBUTION = "contribution"
    REVENUE = "revenue"


@dataclass(frozen=True, slots=True, kw_only=True)
class CostPerLead:
    actual: Measure[Money]
    target: Measure[Money]
    delta_pct: Measure[float]


class BrakeMode(StrEnum):
    AUTONOMOUS = "AUTONOMOUS"
    ALL = "ALL"


@dataclass(frozen=True, slots=True, kw_only=True)
class BrakeState:
    engaged: bool
    mode: BrakeMode | None
    since: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposalCounts:
    pending: int
    deferred: int
    critical: int


@dataclass(frozen=True, slots=True, kw_only=True)
class LeadsAndCustomers:
    today: Measure[int]
    week: Measure[int]


@dataclass(frozen=True, slots=True, kw_only=True)
class PortfolioHeader:
    spend: SpendBreakdown
    caps: Caps
    pacing: Pacing
    projected_month_end: Measure[Money]
    leads: LeadsAndCustomers
    customers: LeadsAndCustomers
    roi: Measure[float]
    roi_basis: RoiBasis
    roi_basis_reason: str | None
    roas: Measure[float]
    cost_per_lead: CostPerLead
    brake: BrakeState
    proposals: ProposalCounts


class SignalKind(StrEnum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    EXIT = "EXIT"


@dataclass(frozen=True, slots=True, kw_only=True)
class RowSignal:
    signal_id: str
    kind: SignalKind
    strength: int
    cause: str
    emitted_at: datetime


class ActionKind(StrEnum):
    APPROVE_INCREASE = "approve_increase"
    APPLY_DECREASE = "apply_decrease"
    APPLY_EXIT = "apply_exit"
    REVIEW_PROPOSAL = "review_proposal"
    NONE = "none"


class ActionMode(StrEnum):
    INLINE_APPROVAL = "inline_approval"
    AUTONOMOUS_APPLIED = "autonomous_applied"
    PROPOSAL = "proposal"
    BLOCKED = "blocked"


class ActionFriction(StrEnum):
    NONE = "none"
    CONFIRM = "confirm"


class BlockedReason(StrEnum):
    BRAKE_ENGAGED = "brake_engaged"
    STALE_DATA = "stale_data"
    GUARDRAIL = "guardrail"
    NOT_CONTROLLABLE = "not_controllable"
    IMMATURE_WINDOW = "immature_window"


@dataclass(frozen=True, slots=True, kw_only=True)
class AppliedChange:
    parameter: str
    before: str
    after: str
    applied_at: datetime
    undo_deadline: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ActionTarget:
    method: str  # "POST" | "PATCH"
    path: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RowAction:
    kind: ActionKind
    mode: ActionMode
    friction: ActionFriction
    requires_evidence: bool
    proposal_id: str | None
    diff_hash: str | None
    applied_change: AppliedChange | None
    blocked_reason: BlockedReason | None
    reason: str | None
    target: ActionTarget | None


@dataclass(frozen=True, slots=True, kw_only=True)
class TickerRow:
    entity_ref: str
    name: str
    level: str  # "campaign" | "ad_set"
    platform: str
    platform_account_id: str
    status: str
    signal: RowSignal | None
    money_at_stake: Money
    expected_contribution_delta: Measure[Money]
    roi: Measure[float]
    roas: Measure[float]
    leads: Measure[int]
    customers: Measure[int]
    customer_value: Measure[Money]
    cost_per_lead: CostPerLead
    spend: Money
    cap: Money | None
    pacing_index_pct: Measure[float]
    sparkline: tuple[float, ...]
    freshness: Freshness
    is_controllable: bool
    is_degraded: bool
    learning_state: LearningState
    action: RowAction


class ChangeKind(StrEnum):
    SIGNAL_CHANGED = "signal_changed"
    ENTITY_ENTERED = "entity_entered"
    ENTITY_EXITED = "entity_exited"
    AUTONOMOUS_APPLIED = "autonomous_applied"
    THRESHOLD_CROSSED = "threshold_crossed"


@dataclass(frozen=True, slots=True, kw_only=True)
class DetailRef:
    """Referencia al detalle de un `ChangeStrip` item -- `{kind, id}`,
    misma forma que `entity_ref` descompone conceptualmente (026, acordado
    con el carril del panel: `detail_ref` no documentaba a que apuntaba)."""

    kind: str  # "signal" | "proposal" | "execution"
    id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangeEntry:
    kind: ChangeKind
    entity_ref: str
    entity_name: str
    before: str | None
    after: str | None
    occurred_at: datetime
    detail_ref: DetailRef | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ChangeStrip:
    since: datetime
    is_partial: bool
    items: tuple[ChangeEntry, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class CockpitView:
    business_id: str
    window: CockpitWindow
    currency: str
    generated_at: datetime
    freshness: Freshness
    is_partial: bool
    degraded_accounts: tuple[DegradedAccount, ...]
    header: PortfolioHeader
    rows: tuple[TickerRow, ...]
    changes_since: ChangeStrip
