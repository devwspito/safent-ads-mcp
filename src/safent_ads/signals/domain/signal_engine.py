"""`signal_engine` (tasks.md T035, plan.md §5): funciones puras que producen
`Signal`/`CreativeSignal` a partir de `MetricWindow` + objetivos + puertas.

Cada `evaluate_<codigo>` encierra una condicion del catalogo
(research/rule-catalog-and-signals.md §2) y devuelve `None` cuando la
condicion no se cumple: quien orquesta (`signals/application`) decide si cae
a `hold_signal`. Representativas cubiertas por test: M05, M07, M09, M13, M16,
M21, G01, G04, G06, X01."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from safent_ads.shared.ids import EntityRef
from safent_ads.signals.domain.cause import Cause, describe
from safent_ads.signals.domain.gate_verdict import GateVerdict
from safent_ads.signals.domain.metric_window import MetricWindow
from safent_ads.signals.domain.signal import CreativeSignal, CreativeSignalKind, Signal, SignalKind
from safent_ads.signals.domain.value_objects import Evidence, MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan

_M07_LOWER_MULTIPLE = 1.5
_M07_UPPER_MULTIPLE = 2.0
_M09_DEFAULT_SPEND_MULTIPLE = 5.0
_M13_FREQUENCY_THRESHOLD = 3.0
_M13_CTR_CEILING = 0.008
_M13_IMPRESSIONS_FLOOR = 8_000
_M16_WARN_DROP_PCT = 0.10
_M21_MIN_IMPRESSIONS_DEFAULT = 1_000
_M21_KILL_HOOK_RATE = 0.20
_M21_SCALE_HOOK_RATE = 0.30
_M21_SCALE_HOLD_RATE = 0.45
_G01_LOST_IS_BUDGET_THRESHOLD_PCT = 20.0
_G01_AT_TARGET_TOLERANCE = 0.05
_G04_CPA_CEILING_RATIO = 1.20
_G06_MIN_CLICKS_DEFAULT = 10
_G06_COST_MULTIPLE = 2.0
_X01_SPIKE_MULTIPLE = 3.0


@dataclass(frozen=True, kw_only=True, slots=True)
class RuleContext:
    """Datos comunes a toda evaluacion de regla sobre una entidad."""

    entity_ref: EntityRef
    currency: str
    gate_verdicts: tuple[GateVerdict, ...]
    as_of: datetime


def hold_signal(context: RuleContext, *, span: WindowSpan) -> Signal:
    """Senal por defecto: puerta bloqueada o ninguna condicion del catalogo
    se cumplio (data-model.md §Signal)."""
    failed = next((v for v in context.gate_verdicts if not v.passed), None)
    if failed is not None:
        cause, sentence = (
            Cause.GATE_BLOCKED,
            describe(Cause.GATE_BLOCKED, gate_reason=failed.reason),
        )
    else:
        cause, sentence = (
            Cause.INSIDE_TARGET_BAND,
            describe(Cause.INSIDE_TARGET_BAND, span=span.value),
        )
    evidence = Evidence(metric="none", actual=0.0, target=None, baseline=None, span=span)
    return _build_signal(
        context,
        kind=SignalKind.HOLD,
        cause=cause,
        cause_sentence=sentence,
        span=span,
        spend_minor=0,
        evidence=evidence,
        rule_code="HOLD",
        strength=SignalStrength(0),
    )


def evaluate_m05(
    context: RuleContext, *, window_3d: MetricWindow, window_7d: MetricWindow, target_roas: float
) -> Signal | None:
    """M05: ROAS por debajo del objetivo en 3D y 7D => SELL -30%, auto."""
    roas_3d, roas_7d = window_3d.roas, window_7d.roas
    if roas_3d is None or roas_7d is None or roas_3d >= target_roas or roas_7d >= target_roas:
        return None
    deviation = (target_roas - roas_7d) / target_roas
    volume = _volume_ratio(window_7d.spend_minor, reference=window_7d.spend_minor)
    evidence = Evidence(
        metric="roas", actual=roas_7d, target=target_roas, baseline=None, span=WindowSpan.D7
    )
    sentence = describe(Cause.ROAS_BELOW_TARGET_SUSTAINED, actual=roas_7d, target=target_roas)
    return _build_signal(
        context,
        kind=SignalKind.SELL,
        cause=Cause.ROAS_BELOW_TARGET_SUSTAINED,
        cause_sentence=sentence,
        span=WindowSpan.D7,
        spend_minor=window_7d.spend_minor,
        evidence=evidence,
        rule_code="M05",
        strength=_strength(deviation, volume),
    )


def evaluate_m07(
    context: RuleContext, *, window_7d: MetricWindow, target_cpa_minor: int
) -> Signal | None:
    """M07: CPA(7D) entre 1.5x y 2x el objetivo, ya post-aprendizaje => EXIT,
    approval."""
    cpa = window_7d.cpa_minor
    if cpa is None or target_cpa_minor <= 0:
        return None
    ratio = cpa / target_cpa_minor
    if not (_M07_LOWER_MULTIPLE <= ratio <= _M07_UPPER_MULTIPLE):
        return None
    volume = _volume_ratio(window_7d.spend_minor, reference=5 * target_cpa_minor)
    evidence = Evidence(
        metric="cpa_minor", actual=cpa, target=target_cpa_minor, baseline=None, span=WindowSpan.D7
    )
    sentence = describe(
        Cause.CPA_ABOVE_TARGET_POST_LEARNING,
        actual=cpa,
        target=target_cpa_minor,
        span=WindowSpan.D7.value,
    )
    return _build_signal(
        context,
        kind=SignalKind.EXIT,
        cause=Cause.CPA_ABOVE_TARGET_POST_LEARNING,
        cause_sentence=sentence,
        span=WindowSpan.D7,
        spend_minor=window_7d.spend_minor,
        evidence=evidence,
        rule_code="M07",
        strength=_strength(ratio - 1.0, volume),
    )


def evaluate_m09(
    context: RuleContext,
    *,
    window_7d: MetricWindow,
    target_cpa_minor: int,
    min_spend_multiple: float = _M09_DEFAULT_SPEND_MULTIPLE,
) -> Signal | None:
    """M09: cero conversiones en 7D con gasto >= k x CPA objetivo => EXIT,
    auto."""
    if window_7d.conversions != 0 or target_cpa_minor <= 0:
        return None
    multiple = window_7d.spend_minor / target_cpa_minor
    if multiple < min_spend_multiple:
        return None
    evidence = Evidence(
        metric="spend_minor",
        actual=window_7d.spend_minor,
        target=None,
        baseline=None,
        span=WindowSpan.D7,
    )
    sentence = describe(
        Cause.ZERO_CONVERSIONS_SPEND_MULTIPLE,
        actual=window_7d.spend_minor,
        multiple=multiple,
        span=WindowSpan.D7.value,
    )
    strength = _strength(multiple / min_spend_multiple - 1.0, 1.0)
    return _build_signal(
        context,
        kind=SignalKind.EXIT,
        cause=Cause.ZERO_CONVERSIONS_SPEND_MULTIPLE,
        cause_sentence=sentence,
        span=WindowSpan.D7,
        spend_minor=window_7d.spend_minor,
        evidence=evidence,
        rule_code="M09",
        strength=strength,
    )


def evaluate_m13(context: RuleContext, *, window_7d: MetricWindow) -> Signal | None:
    """M13: frecuencia > 3.0, CTR < 0.8%, impresiones > 8000 en 7D => EXIT,
    auto (cooldown 24h lo aplica `CooldownGate`)."""
    frequency, ctr = window_7d.frequency, window_7d.ctr
    if frequency is None or ctr is None:
        return None
    triggered = (
        frequency > _M13_FREQUENCY_THRESHOLD
        and ctr < _M13_CTR_CEILING
        and window_7d.impressions > _M13_IMPRESSIONS_FLOOR
    )
    if not triggered:
        return None
    volume = _volume_ratio(window_7d.impressions, reference=_M13_IMPRESSIONS_FLOOR)
    deviation = (frequency - _M13_FREQUENCY_THRESHOLD) / _M13_FREQUENCY_THRESHOLD
    evidence = Evidence(
        metric="frequency",
        actual=frequency,
        target=_M13_FREQUENCY_THRESHOLD,
        baseline=None,
        span=WindowSpan.D7,
    )
    sentence = describe(
        Cause.FREQUENCY_HIGH_CTR_LOW,
        frequency=frequency,
        ctr=ctr,
        impressions=window_7d.impressions,
        span=WindowSpan.D7.value,
    )
    return _build_signal(
        context,
        kind=SignalKind.EXIT,
        cause=Cause.FREQUENCY_HIGH_CTR_LOW,
        cause_sentence=sentence,
        span=WindowSpan.D7,
        spend_minor=window_7d.spend_minor,
        evidence=evidence,
        rule_code="M13",
        strength=_strength(deviation, volume),
    )


def evaluate_m16(
    context: RuleContext, *, window_7d: MetricWindow, window_14d: MetricWindow
) -> CreativeSignal | None:
    """M16: CTR cae >=10% frente a la linea base de 14D => FATIGUE, notify."""
    ctr_7d, ctr_14d = window_7d.ctr, window_14d.ctr
    if ctr_7d is None or not ctr_14d:
        return None
    drop_pct = 1 - (ctr_7d / ctr_14d)
    if drop_pct < _M16_WARN_DROP_PCT:
        return None
    volume = _volume_ratio(window_7d.impressions, reference=window_14d.impressions)
    evidence = Evidence(
        metric="ctr", actual=ctr_7d, target=None, baseline=ctr_14d, span=WindowSpan.D7
    )
    sentence = describe(
        Cause.CTR_DECLINE_VS_BASELINE, actual=ctr_7d, baseline=ctr_14d, drop_pct=drop_pct
    )
    return _build_creative_signal(
        context,
        kind=CreativeSignalKind.FATIGUE,
        cause=Cause.CTR_DECLINE_VS_BASELINE,
        cause_sentence=sentence,
        span=WindowSpan.D7,
        spend_minor=window_7d.spend_minor,
        evidence=evidence,
        rule_code="M16",
        strength=_strength(drop_pct, volume),
    )


def evaluate_m21(
    context: RuleContext,
    *,
    window_7d: MetricWindow,
    min_impressions: int = _M21_MIN_IMPRESSIONS_DEFAULT,
) -> CreativeSignal | None:
    """M21: hook rate < 20% => LOSER (kill); hook >= 30% y hold >= 45% =>
    WINNER (scale), con impresiones minimas."""
    if window_7d.impressions < min_impressions:
        return None
    hook, hold = window_7d.hook_rate, window_7d.hold_rate
    if hook is None:
        return None
    if hook < _M21_KILL_HOOK_RATE:
        return _m21_kill_signal(context, window_7d, hook)
    if hook >= _M21_SCALE_HOOK_RATE and hold is not None and hold >= _M21_SCALE_HOLD_RATE:
        return _m21_scale_signal(context, window_7d, hook, hold)
    return None


def evaluate_g01(
    context: RuleContext,
    *,
    window_7d: MetricWindow,
    target_roas: float,
    at_target_tolerance: float = _G01_AT_TARGET_TOLERANCE,
) -> Signal | None:
    """G01: `Limited by budget` y en objetivo, cuota perdida por presupuesto
    > 20% en 7D => BUY (siempre approval, FR-12: sube gasto)."""
    lost_is_budget, roas = window_7d.search_lost_is_budget_pct, window_7d.roas
    if lost_is_budget is None or roas is None:
        return None
    if lost_is_budget <= _G01_LOST_IS_BUDGET_THRESHOLD_PCT:
        return None
    if roas < target_roas * (1 - at_target_tolerance):
        return None
    volume = _volume_ratio(window_7d.spend_minor, reference=window_7d.spend_minor)
    deviation = (
        lost_is_budget - _G01_LOST_IS_BUDGET_THRESHOLD_PCT
    ) / _G01_LOST_IS_BUDGET_THRESHOLD_PCT
    evidence = Evidence(
        metric="search_lost_is_budget_pct",
        actual=lost_is_budget,
        target=_G01_LOST_IS_BUDGET_THRESHOLD_PCT,
        baseline=None,
        span=WindowSpan.D7,
    )
    sentence = describe(
        Cause.LIMITED_BY_BUDGET_AT_TARGET, actual=lost_is_budget / 100, span=WindowSpan.D7.value
    )
    return _build_signal(
        context,
        kind=SignalKind.BUY,
        cause=Cause.LIMITED_BY_BUDGET_AT_TARGET,
        cause_sentence=sentence,
        span=WindowSpan.D7,
        spend_minor=window_7d.spend_minor,
        evidence=evidence,
        rule_code="G01",
        strength=_strength(deviation, volume),
    )


def evaluate_g04(
    context: RuleContext, *, window_30d: MetricWindow, target_cpa_minor: int
) -> Signal | None:
    """G04: CPA(30D) > 120% del objetivo, post-aprendizaje => SELL/afloja
    objetivo, approval."""
    cpa = window_30d.cpa_minor
    if cpa is None or target_cpa_minor <= 0:
        return None
    ratio = cpa / target_cpa_minor
    if ratio <= _G04_CPA_CEILING_RATIO:
        return None
    volume = _volume_ratio(window_30d.spend_minor, reference=10 * target_cpa_minor)
    excess_pct = ratio - 1.0
    evidence = Evidence(
        metric="cpa_minor", actual=cpa, target=target_cpa_minor, baseline=None, span=WindowSpan.D30
    )
    sentence = describe(
        Cause.CPA_ABOVE_TARGET, actual=cpa, target=target_cpa_minor, excess_pct=excess_pct
    )
    return _build_signal(
        context,
        kind=SignalKind.SELL,
        cause=Cause.CPA_ABOVE_TARGET,
        cause_sentence=sentence,
        span=WindowSpan.D30,
        spend_minor=window_30d.spend_minor,
        evidence=evidence,
        rule_code="G04",
        strength=_strength(excess_pct, volume),
    )


def evaluate_g06(
    context: RuleContext,
    *,
    window_30d: MetricWindow,
    target_cpa_minor: int,
    min_clicks: int = _G06_MIN_CLICKS_DEFAULT,
) -> Signal | None:
    """G06: termino de busqueda con clics >= N, cero conversiones, coste >
    2x tCPA en 30D => EXIT (negativa), auto para no-convertidos."""
    if window_30d.conversions != 0 or window_30d.clicks < min_clicks or target_cpa_minor <= 0:
        return None
    threshold = _G06_COST_MULTIPLE * target_cpa_minor
    if window_30d.spend_minor <= threshold:
        return None
    volume = _volume_ratio(window_30d.clicks, reference=min_clicks * 2)
    evidence = Evidence(
        metric="spend_minor",
        actual=window_30d.spend_minor,
        target=threshold,
        baseline=None,
        span=WindowSpan.D30,
    )
    sentence = describe(
        Cause.SEARCH_TERM_NON_CONVERTING, clicks=window_30d.clicks, actual=window_30d.spend_minor
    )
    deviation = (window_30d.spend_minor - threshold) / threshold
    return _build_signal(
        context,
        kind=SignalKind.EXIT,
        cause=Cause.SEARCH_TERM_NON_CONVERTING,
        cause_sentence=sentence,
        span=WindowSpan.D30,
        spend_minor=window_30d.spend_minor,
        evidence=evidence,
        rule_code="G06",
        strength=_strength(deviation, volume),
    )


def evaluate_x01(
    context: RuleContext, *, today_spend_minor: int, avg_daily_spend_minor: float
) -> Signal | None:
    """X01: gasto de hoy > 3x la media diaria => HOLD (congela BUY), auto
    notify. Sin `MetricWindow`: no es una razon de sumas por ventana sino un
    punto diario contra una media historica."""
    if avg_daily_spend_minor <= 0:
        return None
    multiple = today_spend_minor / avg_daily_spend_minor
    if multiple <= _X01_SPIKE_MULTIPLE:
        return None
    deviation = (multiple - _X01_SPIKE_MULTIPLE) / _X01_SPIKE_MULTIPLE
    evidence = Evidence(
        metric="daily_spend_minor",
        actual=float(today_spend_minor),
        target=avg_daily_spend_minor * _X01_SPIKE_MULTIPLE,
        baseline=avg_daily_spend_minor,
        span=WindowSpan.D3,
    )
    sentence = describe(Cause.DAILY_SPEND_SPIKE, actual=today_spend_minor, multiple=multiple)
    return _build_signal(
        context,
        kind=SignalKind.HOLD,
        cause=Cause.DAILY_SPEND_SPIKE,
        cause_sentence=sentence,
        span=WindowSpan.D3,
        spend_minor=today_spend_minor,
        evidence=evidence,
        rule_code="X01",
        strength=_strength(deviation, 1.0),
    )


def _m21_kill_signal(context: RuleContext, window: MetricWindow, hook: float) -> CreativeSignal:
    volume = _volume_ratio(window.impressions, reference=_M21_MIN_IMPRESSIONS_DEFAULT)
    deviation = (_M21_KILL_HOOK_RATE - hook) / _M21_KILL_HOOK_RATE
    evidence = Evidence(
        metric="hook_rate",
        actual=hook,
        target=_M21_KILL_HOOK_RATE,
        baseline=None,
        span=WindowSpan.D7,
    )
    sentence = describe(Cause.HOOK_RATE_LOW, actual=hook)
    return _build_creative_signal(
        context,
        kind=CreativeSignalKind.LOSER,
        cause=Cause.HOOK_RATE_LOW,
        cause_sentence=sentence,
        span=WindowSpan.D7,
        spend_minor=window.spend_minor,
        evidence=evidence,
        rule_code="M21",
        strength=_strength(deviation, volume),
    )


def _m21_scale_signal(
    context: RuleContext, window: MetricWindow, hook: float, hold: float
) -> CreativeSignal:
    volume = _volume_ratio(window.impressions, reference=_M21_MIN_IMPRESSIONS_DEFAULT)
    deviation = (hook - _M21_SCALE_HOOK_RATE) / _M21_SCALE_HOOK_RATE
    evidence = Evidence(
        metric="hook_rate",
        actual=hook,
        target=_M21_SCALE_HOOK_RATE,
        baseline=None,
        span=WindowSpan.D7,
    )
    sentence = describe(Cause.HOOK_AND_HOLD_RATE_HIGH, hook_rate=hook, hold_rate=hold)
    return _build_creative_signal(
        context,
        kind=CreativeSignalKind.WINNER,
        cause=Cause.HOOK_AND_HOLD_RATE_HIGH,
        cause_sentence=sentence,
        span=WindowSpan.D7,
        spend_minor=window.spend_minor,
        evidence=evidence,
        rule_code="M21",
        strength=_strength(deviation, volume),
    )


def _strength(deviation_ratio: float, data_volume_ratio: float) -> SignalStrength:
    """`strength = f(desviacion del objetivo, volumen de datos)` (tasks.md
    T035): hasta 70 puntos por magnitud de la desviacion, hasta 30 por
    confianza en el volumen."""
    magnitude = min(abs(deviation_ratio), 1.0) * 70
    confidence = min(max(data_volume_ratio, 0.0), 1.0) * 30
    return SignalStrength.clamped(magnitude + confidence)


def _volume_ratio(observed: float, *, reference: float) -> float:
    return 0.0 if reference <= 0 else min(max(observed / reference, 0.0), 1.0)


def _build_signal(
    context: RuleContext,
    *,
    kind: SignalKind,
    cause: Cause,
    cause_sentence: str,
    span: WindowSpan,
    spend_minor: int,
    evidence: Evidence,
    rule_code: str,
    strength: SignalStrength,
) -> Signal:
    return Signal(
        entity_ref=context.entity_ref,
        kind=kind,
        strength=strength,
        cause=cause,
        cause_sentence=cause_sentence,
        span=span,
        money_at_stake=MoneyAtStake(minor_units=spend_minor, currency=context.currency),
        evidence=evidence,
        gate_verdicts=context.gate_verdicts,
        emitted_at=context.as_of,
        rule_code=rule_code,
    )


def _build_creative_signal(
    context: RuleContext,
    *,
    kind: CreativeSignalKind,
    cause: Cause,
    cause_sentence: str,
    span: WindowSpan,
    spend_minor: int,
    evidence: Evidence,
    rule_code: str,
    strength: SignalStrength,
) -> CreativeSignal:
    return CreativeSignal(
        entity_ref=context.entity_ref,
        kind=kind,
        strength=strength,
        cause=cause,
        cause_sentence=cause_sentence,
        span=span,
        money_at_stake=MoneyAtStake(minor_units=spend_minor, currency=context.currency),
        evidence=evidence,
        gate_verdicts=context.gate_verdicts,
        emitted_at=context.as_of,
        rule_code=rule_code,
    )
