"""DTOs de lectura del panel (contracts/rest-api.md §Cartera, §Senales,
§Insignias, T046/T110). `panel` no importa `accounts`/`signals`/... (plan.md
§4): forma propia, declarada aqui; la integracion la rellena desde el
contexto real. `Money`/`Freshness`/`CapsSource`/`Caps`/`Pacing`/
`SpendBreakdown`/`SignalOutcomeStatus`/`SignalOutcome` viven en
`shared.read_models.dto` (simplification-audit.md #9: eran identicos
campo-a-campo a los de `mcp`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from safent_ads.shared.read_models.dto import (
    Caps,
    Freshness,
    Money,
    Pacing,
    SignalOutcome,
    SpendBreakdown,
)


@dataclass(frozen=True, slots=True)
class SignalRef:
    kind: str
    strength: int
    cause: str


@dataclass(frozen=True, slots=True)
class LearningState:
    is_learning: bool
    reason: str | None
    since: datetime | None


@dataclass(frozen=True, slots=True)
class DegradedAccount:
    platform_account_id: str
    platform: str
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class PortfolioRow:
    entity_ref: str
    name: str
    platform: str
    # Canonical `account_ref` (`<platform>:account:<business>:<connection>:<external>`),
    # the same shape `/platform-accounts` returns -- hotfix 0.2.20, Bug C: this used to
    # be the internal `platform_accounts.id` UUID, which never matched `/platform-accounts`
    # and made the panel's account grouping drop every row.
    platform_account_id: str
    # Additive: the internal UUID, kept for anything that still reads it.
    platform_account_uuid: str
    status: str
    currency: str
    budget: Money
    spend: Money
    cost_per_lead: Money | None
    signal: SignalRef | None
    money_at_stake: Money
    is_controllable: bool
    learning_state: LearningState
    spend_14d: list[float]
    is_degraded: bool


@dataclass(frozen=True, slots=True)
class PortfolioView:
    window: str
    currency: str
    spend: SpendBreakdown
    caps: Caps
    pacing: Pacing
    conversions_by_kind: dict[str, int]
    cost_per_lead: Money | None
    cost_per_business_conversion: Money | None
    pending_proposals: int
    deferred_proposals: int
    freshness: Freshness
    deviation_vs_platform_pct: float | None
    is_partial: bool
    degraded_accounts: list[DegradedAccount]
    rows: list[PortfolioRow]


@dataclass(frozen=True, slots=True)
class EntityDetail:
    business_id: str
    entity_ref: str
    name: str
    status: str
    platform_state_hash: str
    is_controllable: bool
    learning_state: str
    can_pause: bool
    can_resume: bool
    can_delete: bool
    level: str = "campaign"


@dataclass(frozen=True, slots=True)
class EntityChild:
    entity_ref: str
    name: str
    level: str
    status: str
    spend_today: Money | None = None
    spend_window: Money | None = None
    conversions_by_kind: dict[str, int] | None = None
    cost_per_lead: Money | None = None
    cost_per_business_conversion: Money | None = None
    signal: SignalRef | None = None
    freshness: Freshness | None = None
    badges: list[str] = field(default_factory=list)
    has_children: bool = False


@dataclass(frozen=True, slots=True)
class MetricPoint:
    period_start: datetime
    spend: Money
    conversions: int


@dataclass(frozen=True, slots=True)
class EntityMetrics:
    entity_ref: str
    granularity: str
    points: list[MetricPoint]


@dataclass(frozen=True, slots=True)
class EntityHistoryEntry:
    occurred_at: datetime
    origin: str
    parametro: str
    valor_anterior: str
    valor_nuevo: str


class ActionTaken(StrEnum):
    AUTONOMOUS = "autonomous"
    PROPOSAL = "proposal"
    NO_ACTION = "none"


@dataclass(frozen=True, slots=True)
class SignalView:
    signal_id: str
    business_id: str
    entity_ref: str
    entity_name: str
    platform: str
    kind: str
    strength: int
    cause: str
    money_at_stake: Money
    data_window: str
    action_taken: ActionTaken
    proposal_id: str | None
    emitted_at: datetime
    outcome: SignalOutcome


@dataclass(frozen=True, slots=True)
class SignalDetailView:
    signal: SignalView
    gate_verdicts: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    narrative: str = ""


@dataclass(frozen=True, slots=True)
class SignalsPage:
    items: list[SignalView]
    next_cursor: str | None
    confirmed_rate_pct: float | None
    confirmed_rate_sample: int


@dataclass(frozen=True, slots=True)
class AnomalyView:
    anomaly_id: str
    business_id: str
    entity_ref: str
    method: str
    score: float
    severity: str


@dataclass(frozen=True, slots=True)
class PacingView:
    entity_ref: str
    business_id: str
    pace_index: float
    projected: Money
    remaining: Money
    new_daily: Money


class ConnectionLevel(StrEnum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


class BrakeMode(StrEnum):
    AUTONOMOUS = "AUTONOMOUS"
    ALL = "ALL"


@dataclass(frozen=True, slots=True)
class ProposalBadges:
    pending: int
    critical: int
    deferred: int


@dataclass(frozen=True, slots=True)
class CreativeBadges:
    pending_approval: int


@dataclass(frozen=True, slots=True)
class SignalBadges:
    new_since: int
    since: datetime


@dataclass(frozen=True, slots=True)
class ConnectionsBadge:
    level: ConnectionLevel
    reason: str | None


@dataclass(frozen=True, slots=True)
class BrakeBadge:
    engaged: bool
    mode: BrakeMode | None


@dataclass(frozen=True, slots=True)
class Badges:
    proposals: ProposalBadges
    creatives: CreativeBadges
    signals: SignalBadges
    connections: ConnectionsBadge
    brake: BrakeBadge
