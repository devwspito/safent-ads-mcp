"""DTOs de lectura del catalogo MCP (contracts/mcp-tools.md §Tipos comunes
y §Lecturas). `mcp` no importa `accounts`/`signals`/`rules`/... (plan.md §4:
grafo aciclico): cada DTO es la forma que `mcp` necesita, declarada aqui, y
la integracion la rellena desde el contexto real. Dataclasses planas, sin
herencia entre ellas: cada "detalle" repite sus campos en lugar de heredar,
para no arrastrar el acoplamiento fragil de la herencia de dataclasses.
`Money`/`Freshness`/`CapsSource`/`Caps`/`Pacing`/`SpendBreakdown`/
`SignalOutcomeStatus`/`SignalOutcome` viven en `shared.read_models.dto`
(simplification-audit.md #9: eran identicos campo-a-campo a los de
`panel`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from safent_ads.shared.read_models.dto import (
    Caps,
    Freshness,
    Money,
    Pacing,
    SignalOutcome,
    SpendBreakdown,
)


class PlatformCode(StrEnum):
    GOOGLE = "google"
    META = "meta"


class EntityLevel(StrEnum):
    ACCOUNT = "account"
    CAMPAIGN = "campaign"
    AD_SET = "ad_set"
    AD = "ad"
    CREATIVE = "creative"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    THROTTLED = "throttled"
    SUSPENDED = "suspended"
    READ_ONLY = "read_only"


class CampaignStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    REMOVED = "removed"


class SignalKind(StrEnum):
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"
    EXIT = "exit"


class WindowPreset(StrEnum):
    TODAY = "T"
    THREE_DAYS = "3D"
    SEVEN_DAYS = "7D"
    FOURTEEN_DAYS = "14D"
    THIRTY_DAYS = "30D"
    MONTH_TO_DATE = "MTD"


class Granularity(StrEnum):
    DAILY = "daily"
    HOURLY = "hourly"


class AutonomyLevel(StrEnum):
    NOTIFY = "notify"
    AUTO = "auto"
    APPROVAL = "approval"


class ProposalState(StrEnum):
    """10 estados, alineados byte a byte con `proposals.domain.proposal.
    ProposalState` (identificadores tecnicos en ingles, NFR-12): sin esta
    igualdad, `list_proposals` no puede filtrar por `state` contra
    `proposals.state` en la base (bug encontrado en esta lane -- el DTO
    tenia valores en castellano mientras el dominio y la migracion `0008`
    ya usaban ingles)."""

    PENDING = "pending"
    POSTPONED = "postponed"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    INVALIDATED = "invalidated"


class MediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class Window:
    """Refleja `contracts/mcp-tools.md`: o bien `preset` (+`lag_days`), o
    bien un rango explicito `from`/`to`; nunca ambos (validado en
    `presentation/args.py`, no aqui — este DTO ya llega bien formado)."""

    preset: WindowPreset | None
    lag_days: int
    date_from: date | None
    date_to: date | None


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    cursor: str | None


# --- portfolio ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BusinessSummary:
    business_id: str
    name: str
    timezone: str
    currency: str


@dataclass(frozen=True, slots=True)
class PlatformAccountSummary:
    account_ref: str
    platform: PlatformCode
    currency: str
    timezone: str
    status: AccountStatus
    api_tier: str


@dataclass(frozen=True, slots=True)
class AccountFreshness:
    account_ref: str
    freshness: Freshness


@dataclass(frozen=True, slots=True)
class TopMover:
    entity_ref: str
    name: str
    delta_pct: float
    metric: str


@dataclass(frozen=True, slots=True)
class DegradedAccountSummary:
    platform_account_id: str
    platform: PlatformCode
    status: AccountStatus
    reason: str


@dataclass(frozen=True, slots=True)
class PortfolioOverview:
    """Mirror de `contracts/rest-api.md` GET /portfolio (T110): mismo
    desglose spend/caps/pacing y degradacion por cuenta que `panel`, cada
    contexto con su propio DTO (plan.md §4, sin import cruzado)."""

    spend: SpendBreakdown
    caps: Caps
    pacing: Pacing
    conversions_by_kind: dict[str, int]
    cost_per_lead: Money | None
    cost_per_business_conversion: Money | None
    freshness: Freshness
    is_partial: bool
    degraded_accounts: list[DegradedAccountSummary]
    top_movers: list[TopMover]


# --- entities / metrics -----------------------------------------------


@dataclass(frozen=True, slots=True)
class CampaignSummary:
    entity_ref: str
    name: str
    status: CampaignStatus
    budget: Money
    learning_state: str
    is_controllable: bool
    signal_kind: SignalKind


@dataclass(frozen=True, slots=True)
class EntitySummary:
    entity_ref: str
    name: str
    level: EntityLevel
    status: CampaignStatus
    is_controllable: bool


@dataclass(frozen=True, slots=True)
class CreativeSummary:
    asset_id: str
    media_kind: MediaKind
    format: str
    in_use_by: list[str]
    policy_verdict: str


@dataclass(frozen=True, slots=True)
class CreativeDetail:
    summary: CreativeSummary
    signal_id: str | None


@dataclass(frozen=True, slots=True)
class MetricPoint:
    period_start: datetime
    spend: Money
    conversions: int
    cost_per_conversion: Money


@dataclass(frozen=True, slots=True)
class MetricsSeries:
    entity_ref: str
    granularity: Granularity
    points: list[MetricPoint]


@dataclass(frozen=True, slots=True)
class InsightsSnapshot:
    entity_ref: str
    fetched_at: datetime
    cached_ttl_seconds: int
    breakdown: dict[str, float]


@dataclass(frozen=True, slots=True)
class GaqlResult:
    account_ref: str
    rows: list[dict[str, str]]
    row_count: int


# --- signals / anomalies / pacing --------------------------------------


@dataclass(frozen=True, slots=True)
class Cause:
    text: str
    signal_id: str | None
    rule_id: str | None


@dataclass(frozen=True, slots=True)
class Evidence:
    metric: str
    actual: float
    target: float
    window_label: str


@dataclass(frozen=True, slots=True)
class SignalSummary:
    signal_id: str
    entity_ref: str
    kind: SignalKind
    strength: int
    cause: Cause
    money_at_stake: Money
    window_label: str
    outcome: SignalOutcome


@dataclass(frozen=True, slots=True)
class GateVerdict:
    gate_name: str
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SignalDetail:
    summary: SignalSummary
    gate_verdicts: list[GateVerdict] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    narrative: str = ""


@dataclass(frozen=True, slots=True)
class SignalsPage:
    """Mirror de `SignalsPage` de `panel` (T110): la cabecera no muestra
    tasa con muestra <10 (contracts/rest-api.md)."""

    items: list[SignalSummary]
    cursor: str | None
    confirmed_rate_pct: float | None
    confirmed_rate_sample: int


@dataclass(frozen=True, slots=True)
class AnomalySummary:
    anomaly_id: str
    entity_ref: str
    method: str
    score: float
    severity: str


@dataclass(frozen=True, slots=True)
class PacingInfo:
    entity_ref: str
    pace_index: float
    projected: Money
    remaining: Money
    new_daily: Money


# --- rules / guardrails ------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleSummary:
    rule_id: str
    code: str
    platform: PlatformCode | None
    enabled: bool
    autonomy_level: AutonomyLevel


@dataclass(frozen=True, slots=True)
class RuleDetail:
    summary: RuleSummary
    condition: str
    window_label: str
    action: str
    magnitude_pct: float | None
    cooldown_hours: int


@dataclass(frozen=True, slots=True)
class RuleExplanation:
    rule_id: str
    would_fire: bool
    reason: str
    projected_diff: dict[str, str]


@dataclass(frozen=True, slots=True)
class GuardrailInfo:
    scope_ref: str
    daily_cap: Money
    monthly_cap: Money
    budget_floor: Money
    budget_ceiling: Money
    max_step_pct: float
    max_changes_per_entity_per_day: int


@dataclass(frozen=True, slots=True)
class KillSwitchStatus:
    engaged: bool
    scope: str
    mode: str
    reason: str | None
    since: datetime | None


# --- proposals -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposedDiff:
    parametro: str
    valor_actual: str
    valor_propuesto: str
    diff_hash: str


@dataclass(frozen=True, slots=True)
class ProposalSummary:
    proposal_id: str
    entity_ref: str
    diff: ProposedDiff
    classification: str
    urgency: str
    estimated_impact: Money
    expires_at: datetime
    estado: ProposalState


@dataclass(frozen=True, slots=True)
class ProposalDetail:
    summary: ProposalSummary
    evidence: list[Evidence]
    signal_id: str | None
    rule_id: str | None
    guardrails_applicable: list[str]


# --- catalog ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OfferingSummary:
    offering_id: str
    name: str
    price: Money | None = None


@dataclass(frozen=True, slots=True)
class CalendarEventSummary:
    calendar_event_id: str
    offering_id: str
    name: str
    kind: str
    is_window_open: bool


@dataclass(frozen=True, slots=True)
class CalendarEventDetail:
    summary: CalendarEventSummary
    window_start: date
    window_end: date
    event_date: date | None
    region: str | None


# --- audit / decision log -------------------------------------------------


@dataclass(frozen=True, slots=True)
class DecisionLogEntrySummary:
    seq: int
    occurred_at: datetime
    event_type: str
    entity_ref: str | None
    entry_hash: str


@dataclass(frozen=True, slots=True)
class DecisionLogEntryDetail:
    summary: DecisionLogEntrySummary
    payload: dict[str, str]
    prev_hash: str


# --- creative ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreativeBriefSummary:
    brief_id: str
    signal_id: str | None
    status: str


@dataclass(frozen=True, slots=True)
class CreativeJobStatus:
    job_id: str
    state: str
    progress: float
    renderer_used: str | None
    asset_ids: list[str]


# --- brand -----------------------------------------------------------------


class BrandAssetKind(StrEnum):
    LOGO_VECTOR = "logo_vector"
    LOGO_RASTER = "logo_raster"
    REFERENCE_PHOTO = "reference_photo"
    ICON = "icon"


@dataclass(frozen=True, slots=True)
class BrandAssetSummary:
    asset_id: str
    kind: str
    url: str
    usage: str


@dataclass(frozen=True, slots=True)
class TypographyDetail:
    primary_family: str
    secondary_family: str | None
    licence_note: str
    weights: list[str]


@dataclass(frozen=True, slots=True)
class ColorSwatchDetail:
    role: str
    hex: str
    contrast_ratio_on_white: float
    meets_wcag_aa_normal_text: bool


@dataclass(frozen=True, slots=True)
class ToneOfVoiceDetail:
    description: str
    adjectives: list[str]
    avoid: list[str]


@dataclass(frozen=True, slots=True)
class LegalDisclaimerDetail:
    text: str
    applies_to: list[str] | None


@dataclass(frozen=True, slots=True)
class PlatformConstraintDetail:
    platform: str
    max_headline_chars: int | None
    requires_disclaimer: bool
    notes: str


@dataclass(frozen=True, slots=True)
class BrandKitDetail:
    brand_kit_id: str
    business_id: str
    typography: TypographyDetail
    palette: list[ColorSwatchDetail]
    tone_of_voice: ToneOfVoiceDetail
    assets: list[BrandAssetSummary]
    claims_allowlist: list[str]
    forbidden_claims: list[str]
    legal_disclaimers: list[LegalDisclaimerDetail]
    platform_constraints: list[PlatformConstraintDetail]
    is_complete: bool
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class BrandKitSummary:
    """Version compacta de `BrandKitDetail` para incrustar en
    `ProjectContextPack` sin inflar su tamano (contracts internos de esta
    lane: "cap the payload size")."""

    logos_count: int
    has_typography: bool
    has_palette: bool
    has_tone_of_voice: bool
    forbidden_claims_count: int
    legal_disclaimers_count: int
    is_complete: bool
    updated_at: datetime


# --- capabilities ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AutonomyStatus:
    enabled: bool
    reason: str | None
    scope: str = "automatic_defensive_rule_execution"


@dataclass(frozen=True, slots=True)
class PlatformWriteCapability:
    platform: PlatformCode
    writes_available: bool
    reason: str
    scope: str = "direct_platform_writes"


@dataclass(frozen=True, slots=True)
class GenerationBackendCapability:
    capability: str
    configured: bool
    backend: str | None
    optional: bool = True
    required_for_campaign_drafts: bool = False


@dataclass(frozen=True, slots=True)
class CapabilitiesReport:
    business_id: str
    platform_writes: list[PlatformWriteCapability]
    generation_backends: list[GenerationBackendCapability]
    autonomy: AutonomyStatus
    missing_credentials: list[str]
    generated_at: datetime
    optional_missing_credentials: tuple[str, ...] = ()
    interpretation: tuple[str, ...] = (
        "platform_writes describes direct provider writes, not proposal creation. "
        "Use the registered propose_* tools to prepare changes for human review; "
        "approval and execution still validate permissions, account limits and provider support.",
        "autonomy describes defensive AUTO rule execution, not the ability to analyze "
        "metrics or prepare proposals. It does not certify that a 24/7 agent is running.",
        "Optional image, video, voice and web-search keys are not prerequisites for "
        "writing ad copy, using existing assets or saving incomplete campaign drafts.",
    )


# --- project context ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InventorySummary:
    total_campaigns: int
    active_campaigns: int
    paused_campaigns: int
    learning_campaigns: int
    controllable_campaigns: int
    truncated: bool
    note: str


@dataclass(frozen=True, slots=True)
class ProjectContextPack:
    """Todo lo que el agente necesita en una sola llamada para "hazme unos
    banners para una campana de Display de X" (tool-surface.md, encargo de
    esta lane). Ensamblado, no almacenado: cada campo viene de un puerto de
    lectura ya existente (`ReadModelPorts`), nunca de un almacen propio."""

    business: BusinessSummary
    platform_accounts: list[PlatformAccountSummary]
    open_calendar_events: list[CalendarEventDetail]
    inventory: InventorySummary
    top_performers: list[TopMover]
    bottom_performers: list[TopMover]
    signals: list[SignalSummary]
    open_proposals: list[ProposalSummary]
    autonomy: AutonomyStatus
    guardrails: list[GuardrailInfo]
    brand_kit: BrandKitSummary | None
    offerings: list[OfferingSummary]
    freshness: list[AccountFreshness]
    capability_notes: list[str]
    generated_at: datetime
