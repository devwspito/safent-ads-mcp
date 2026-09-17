"""`diagnose(entity, windows, economics, benchmarks) -> DiagnosisPath`
(profitability-engine.md §5): arbol de 9 nodos, orden no negociable -- un
fallo de medicion hace mentir a lo demas.

Layout y scoring de `google-ads-audit`, nodo 3 delegado en `arba`
(reutilizados como referencia de umbrales, no como dependencia de codigo:
ninguno de los dos publica un paquete Python instalable via PyPI en este
momento -- se documenta el mapeo a sus check-id para cuando el pack de
`weekly-account-audit` (T172) los traiga). Nodos `[build]`: 1, 6, 8 -- sin
base OSS, huecos que profitability-engine.md marca explicitamente."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

UNATTRIBUTED_SHARE_THRESHOLD = 0.35
DELTA_HAT_SANITY_LOW = 0.6
DELTA_HAT_SANITY_HIGH = 1.6
LOST_IS_BUDGET_THRESHOLD = 0.20
LOST_IS_RANK_THRESHOLD = 0.30
CPM_SPIKE_VS_14D_THRESHOLD = 0.25
CTR_VS_MEDIAN90D_THRESHOLD = 0.70
LANDING_CTR_TO_LEAD_VS_MEDIAN90D_THRESHOLD = 0.70
OFFER_LEAD_TO_BUSINESS_CONVERSION_VS_MEDIAN_THRESHOLD = 0.70
COLD_FREQUENCY_THRESHOLD = 3.0
RETARGETING_FREQUENCY_THRESHOLD = 6.0
COHORT_MATURITY_THRESHOLD = 0.60
SEASONALITY_SIGMA_BAND = 1.0


class DiagnosisNodeName(StrEnum):
    MEASUREMENT = "measurement"
    FRESHNESS_AND_STATE = "freshness_and_state"
    VOLUME_AND_AUCTION = "volume_and_auction"
    RELEVANCE = "relevance"
    LANDING = "landing"
    OFFER_AND_PRICE = "offer_and_price"
    SATURATION = "saturation"
    ATTRIBUTION_LAG = "attribution_lag"
    SEASONALITY = "seasonality"


class DiagnosisAction(StrEnum):
    FREEZE_BUY_AND_FIX = "freeze_buy_and_fix"
    HOLD = "hold"
    BUY_CANDIDATE_FIX_BID_OR_QUALITY = "buy_candidate_fix_bid_or_quality"
    FIX_CREATIVE_OR_KEYWORDS = "fix_creative_or_keywords"
    NOTIFY_LANDING_OUT_OF_SCOPE = "notify_landing_out_of_scope"
    REPORT_TO_OWNER_NEVER_TOUCH_BID = "report_to_owner_never_touch_bid"
    EXPAND_AUDIENCE_OR_ROTATE = "expand_audience_or_rotate"
    FALSE_NEGATIVE_DO_NOT_ACT = "false_negative_do_not_act"
    HOLD_SEASONAL = "hold_seasonal"


@dataclass(frozen=True, kw_only=True, slots=True)
class EntityDiagnosisMetrics:
    """Entrada de `diagnose()`: metricas ya resueltas por quien orquesta
    (capa anticorrupcion -- este modulo no conoce `accounts`/`metrics`)."""

    unattributed_share: float
    delta_hat: float | None
    utm_valid: bool
    bridge_has_recent_events_24h: bool
    is_stale: bool
    is_suspended: bool
    is_drifted: bool
    is_learning: bool
    lost_is_budget_pct: float | None
    lost_is_rank_pct: float | None
    cpm_change_vs_14d_pct: float | None
    ctr_7d_vs_median90d_ratio: float | None
    quality_score_below_average: bool
    click_to_lead_vs_median90d_ratio: float | None
    lead_to_business_conversion_vs_offering_median_ratio: float | None
    frequency: float | None
    is_retargeting_audience: bool
    ctr_declining: bool
    cpm_rising: bool
    cohort_maturity: float | None
    projected_cpe_meets_target: bool | None
    within_seasonality_band: bool | None


@dataclass(frozen=True, kw_only=True, slots=True)
class DiagnosisNodeResult:
    order: int
    name: DiagnosisNodeName
    matched: bool
    action: DiagnosisAction | None
    reason: str
    reference_check_ids: tuple[str, ...]


@dataclass(frozen=True, kw_only=True, slots=True)
class DiagnosisPath:
    """El camino completo, no solo el primer nodo (§5: 'se adjunta como
    evidence')."""

    nodes: tuple[DiagnosisNodeResult, ...]

    @property
    def first_match(self) -> DiagnosisNodeResult | None:
        return next((n for n in self.nodes if n.matched), None)

    @property
    def action(self) -> DiagnosisAction:
        match = self.first_match
        return match.action if match is not None and match.action is not None else _NO_MATCH_ACTION


_NO_MATCH_ACTION = DiagnosisAction.HOLD


class _NodeOrder(IntEnum):
    MEASUREMENT = 1
    FRESHNESS_AND_STATE = 2
    VOLUME_AND_AUCTION = 3
    RELEVANCE = 4
    LANDING = 5
    OFFER_AND_PRICE = 6
    SATURATION = 7
    ATTRIBUTION_LAG = 8
    SEASONALITY = 9


def is_measurement_broken(
    *,
    unattributed_share: float,
    delta_hat: float | None,
    utm_valid: bool,
    bridge_has_recent_events_24h: bool,
) -> bool:
    """Nodo 1, extraido a funcion publica (T158) para que `orchestration`
    pueda preguntarle lo mismo que el arbol completo a nivel de CUENTA, sin
    tener que construir un `EntityDiagnosisMetrics` completo con los otros
    21 campos que no le hacen falta para decidir si congelar BUY."""
    broken_delta = delta_hat is not None and not (
        DELTA_HAT_SANITY_LOW <= delta_hat <= DELTA_HAT_SANITY_HIGH
    )
    return (
        unattributed_share > UNATTRIBUTED_SHARE_THRESHOLD
        or broken_delta
        or not utm_valid
        or not bridge_has_recent_events_24h
    )


def _node_1_measurement(m: EntityDiagnosisMetrics) -> DiagnosisNodeResult:
    matched = is_measurement_broken(
        unattributed_share=m.unattributed_share,
        delta_hat=m.delta_hat,
        utm_valid=m.utm_valid,
        bridge_has_recent_events_24h=m.bridge_has_recent_events_24h,
    )
    return DiagnosisNodeResult(
        order=_NodeOrder.MEASUREMENT,
        name=DiagnosisNodeName.MEASUREMENT,
        matched=matched,
        action=DiagnosisAction.FREEZE_BUY_AND_FIX if matched else None,
        reason=(
            f"unattributed_share={m.unattributed_share:.2f}, delta_hat={m.delta_hat}, "
            f"utm_valid={m.utm_valid}, bridge_24h={m.bridge_has_recent_events_24h}"
        ),
        reference_check_ids=("build:measurement",),
    )


def _node_2_freshness_and_state(m: EntityDiagnosisMetrics) -> DiagnosisNodeResult:
    matched = m.is_stale or m.is_suspended or m.is_drifted or m.is_learning
    return DiagnosisNodeResult(
        order=_NodeOrder.FRESHNESS_AND_STATE,
        name=DiagnosisNodeName.FRESHNESS_AND_STATE,
        matched=matched,
        action=DiagnosisAction.HOLD if matched else None,
        reason=(
            f"is_stale={m.is_stale}, is_suspended={m.is_suspended}, "
            f"is_drifted={m.is_drifted}, is_learning={m.is_learning}"
        ),
        reference_check_ids=("signals:existing_gates",),
    )


def _node_3_volume_and_auction(m: EntityDiagnosisMetrics) -> DiagnosisNodeResult:
    lost_budget = m.lost_is_budget_pct is not None and m.lost_is_budget_pct > (
        LOST_IS_BUDGET_THRESHOLD
    )
    lost_rank = m.lost_is_rank_pct is not None and m.lost_is_rank_pct > LOST_IS_RANK_THRESHOLD
    cpm_spike = (
        m.cpm_change_vs_14d_pct is not None and m.cpm_change_vs_14d_pct > CPM_SPIKE_VS_14D_THRESHOLD
    )
    matched = lost_budget or lost_rank or cpm_spike
    return DiagnosisNodeResult(
        order=_NodeOrder.VOLUME_AND_AUCTION,
        name=DiagnosisNodeName.VOLUME_AND_AUCTION,
        matched=matched,
        action=DiagnosisAction.BUY_CANDIDATE_FIX_BID_OR_QUALITY if matched else None,
        reason=(
            f"lost_is_budget={m.lost_is_budget_pct}, lost_is_rank={m.lost_is_rank_pct}, "
            f"cpm_change_14d={m.cpm_change_vs_14d_pct}"
        ),
        reference_check_ids=("arba:impression_share_lost",),
    )


def _node_4_relevance(m: EntityDiagnosisMetrics) -> DiagnosisNodeResult:
    ctr_low = (
        m.ctr_7d_vs_median90d_ratio is not None
        and m.ctr_7d_vs_median90d_ratio < CTR_VS_MEDIAN90D_THRESHOLD
    )
    matched = ctr_low or m.quality_score_below_average
    return DiagnosisNodeResult(
        order=_NodeOrder.RELEVANCE,
        name=DiagnosisNodeName.RELEVANCE,
        matched=matched,
        action=DiagnosisAction.FIX_CREATIVE_OR_KEYWORDS if matched else None,
        reason=(
            f"ctr_ratio={m.ctr_7d_vs_median90d_ratio}, "
            f"quality_score_below_average={m.quality_score_below_average}"
        ),
        reference_check_ids=("google-ads-audit:quality_score", "optmyzr:ctr"),
    )


def _node_5_landing(m: EntityDiagnosisMetrics) -> DiagnosisNodeResult:
    matched = (
        m.click_to_lead_vs_median90d_ratio is not None
        and m.click_to_lead_vs_median90d_ratio < LANDING_CTR_TO_LEAD_VS_MEDIAN90D_THRESHOLD
        and not _ctr_is_abnormal(m)
    )
    return DiagnosisNodeResult(
        order=_NodeOrder.LANDING,
        name=DiagnosisNodeName.LANDING,
        matched=matched,
        action=DiagnosisAction.NOTIFY_LANDING_OUT_OF_SCOPE if matched else None,
        reason=f"click_to_lead_ratio={m.click_to_lead_vs_median90d_ratio}",
        reference_check_ids=("build:landing",),
    )


def _ctr_is_abnormal(m: EntityDiagnosisMetrics) -> bool:
    return (
        m.ctr_7d_vs_median90d_ratio is not None
        and m.ctr_7d_vs_median90d_ratio < CTR_VS_MEDIAN90D_THRESHOLD
    )


def _node_6_offer_and_price(m: EntityDiagnosisMetrics) -> DiagnosisNodeResult:
    """Jamas propone puja (§5): la accion es siempre un informe al dueño."""
    matched = (
        m.lead_to_business_conversion_vs_offering_median_ratio is not None
        and m.lead_to_business_conversion_vs_offering_median_ratio
        < (OFFER_LEAD_TO_BUSINESS_CONVERSION_VS_MEDIAN_THRESHOLD)
        and not _click_to_lead_is_abnormal(m)
    )
    return DiagnosisNodeResult(
        order=_NodeOrder.OFFER_AND_PRICE,
        name=DiagnosisNodeName.OFFER_AND_PRICE,
        matched=matched,
        action=DiagnosisAction.REPORT_TO_OWNER_NEVER_TOUCH_BID if matched else None,
        reason=(
            "lead_to_business_conversion_ratio="
            f"{m.lead_to_business_conversion_vs_offering_median_ratio}"
        ),
        reference_check_ids=("build:offer_and_price",),
    )


def _click_to_lead_is_abnormal(m: EntityDiagnosisMetrics) -> bool:
    return (
        m.click_to_lead_vs_median90d_ratio is not None
        and m.click_to_lead_vs_median90d_ratio < LANDING_CTR_TO_LEAD_VS_MEDIAN90D_THRESHOLD
    )


def _node_7_saturation(m: EntityDiagnosisMetrics) -> DiagnosisNodeResult:
    threshold = (
        RETARGETING_FREQUENCY_THRESHOLD if m.is_retargeting_audience else (COLD_FREQUENCY_THRESHOLD)
    )
    matched = (
        m.frequency is not None and m.frequency > threshold and m.ctr_declining and m.cpm_rising
    )
    return DiagnosisNodeResult(
        order=_NodeOrder.SATURATION,
        name=DiagnosisNodeName.SATURATION,
        matched=matched,
        action=DiagnosisAction.EXPAND_AUDIENCE_OR_ROTATE if matched else None,
        reason=f"frequency={m.frequency}, threshold={threshold}",
        reference_check_ids=("rules:M13", "rules:M14", "rules:M16"),
    )


def _node_8_attribution_lag(m: EntityDiagnosisMetrics) -> DiagnosisNodeResult:
    matched = (
        m.cohort_maturity is not None
        and m.cohort_maturity < COHORT_MATURITY_THRESHOLD
        and m.projected_cpe_meets_target is True
    )
    return DiagnosisNodeResult(
        order=_NodeOrder.ATTRIBUTION_LAG,
        name=DiagnosisNodeName.ATTRIBUTION_LAG,
        matched=matched,
        action=DiagnosisAction.FALSE_NEGATIVE_DO_NOT_ACT if matched else None,
        reason=(
            f"maturity={m.cohort_maturity}, "
            f"projected_cpe_meets_target={m.projected_cpe_meets_target}"
        ),
        reference_check_ids=("build:attribution_lag",),
    )


def _node_9_seasonality(m: EntityDiagnosisMetrics) -> DiagnosisNodeResult:
    matched = m.within_seasonality_band is True
    return DiagnosisNodeResult(
        order=_NodeOrder.SEASONALITY,
        name=DiagnosisNodeName.SEASONALITY,
        matched=matched,
        action=DiagnosisAction.HOLD_SEASONAL if matched else None,
        reason=f"within_seasonality_band={m.within_seasonality_band}",
        reference_check_ids=("google-ads-audit:seasonality",),
    )


_NODE_BUILDERS = (
    _node_1_measurement,
    _node_2_freshness_and_state,
    _node_3_volume_and_auction,
    _node_4_relevance,
    _node_5_landing,
    _node_6_offer_and_price,
    _node_7_saturation,
    _node_8_attribution_lag,
    _node_9_seasonality,
)


def diagnose(metrics: EntityDiagnosisMetrics) -> DiagnosisPath:
    """Evalua los 9 nodos EN ORDEN y devuelve el camino completo; el primer
    nodo que coincide (`first_match`) determina la accion -- un fallo de
    medicion (nodo 1) precede a cualquier otro diagnostico."""
    return DiagnosisPath(nodes=tuple(builder(metrics) for builder in _NODE_BUILDERS))
