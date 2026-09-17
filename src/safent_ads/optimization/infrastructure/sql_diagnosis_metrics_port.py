"""`DiagnosisMetricsPort` real (profitability-engine.md §5, capa
anticorrupcion sobre `accounts`/`metrics`/`execution`/`crm`/`economics`):
ensambla `EntityDiagnosisMetrics` desde `ad_entities`, `metrics_daily`
(0004_metrics_facts, "CTR/CPA/... se derivan como razon de sumas sobre la
ventana", el mismo criterio que aqui), `data_freshness`
(`execution.infrastructure.sql_freshness.SqlFreshnessPort`, reutilizado tal
cual), `lead_attributions` (T156/T158: `unattributed_share`, la fraccion de
conversiones resueltas como `aggregate`) y `platform_divergence_snapshots`
(T156/T158: `delta_hat`, el ultimo snapshot de la cuenta de la entidad).

Alcance reducido a proposito (Assumption documentada, escalado a tech-lead/
database-engineer -- `composition/economics_rest.py` ya llamaba a esto
"trabajo de dominio, no de cableado"): TRES campos NO opcionales del
dominio siguen sin fuente real en el esquema -- `utm_valid`/
`bridge_has_recent_events_24h` (necesitan un registro de eventos del
puente UTM/pixel que no existe: `validate_utm_consistency`, T159, no tiene
todavia de donde leer una URL observada por anuncio) y `quality_score
_below_average`/`is_retargeting_audience` (Quality Score y metadato de
segmentacion, sin ingesta todavia). Quedan en el valor que NO hace saltar
el arbol de diagnostico por un falso positivo (nodo 1 `MEASUREMENT` no
dispara con datos que no se han medido de verdad): nunca un numero
inventado que aparente ser una medicion (§7 del propio dominio: "no hay
numero"). El resto de campos (16 de 22, tras T158) es dato real."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.infrastructure.sql_freshness import SqlFreshnessPort
from safent_ads.optimization.domain.diagnosis import EntityDiagnosisMetrics
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId, EntityRef

__all__ = ["SqlDiagnosisMetricsPort"]

_RECENT_WINDOW_DAYS: Final = 7
_TREND_BASELINE_DAYS: Final = 14
_RATIO_BASELINE_DAYS: Final = 90
# T158: ventana de `unattributed_share` -- lo bastante amplia para no
# saltar por un dia sin CRM, lo bastante corta para reflejar un puente roto
# reciente (mismo orden de magnitud que `_RATIO_BASELINE_DAYS`).
_UNATTRIBUTED_SHARE_WINDOW_DAYS: Final = 30

# Sin conversiones en la ventana no hay nada que atribuir mal todavia: cero
# es el valor que no hace saltar el nodo 1 por un falso positivo (§7: "no
# hay numero" no aplica aqui porque 0 conversiones SI es un dato real).
_NO_MEASUREMENT_GAP_DETECTED: Final = 0.0

_ENTITY_ROW = text("""
    SELECT status, learning_state, platform_account_id
      FROM ad_entities WHERE entity_ref = :entity_ref
""")

_LATEST_DIVERGENCE = text("""
    SELECT value FROM platform_divergence_snapshots
     WHERE business_id = :business_id AND platform_account_id = :platform_account_id
     ORDER BY computed_at DESC
     LIMIT 1
""")

_UNATTRIBUTED_SHARE = text("""
    SELECT count(*) FILTER (WHERE attribution_rung = 'aggregate')::float
           / NULLIF(count(*), 0) AS share
      FROM lead_attributions
     WHERE business_id = :business_id
       AND occurred_at >= :start_date AND occurred_at < :end_date
""")

_WINDOW_AGGREGATE = text("""
    SELECT coalesce(sum(spend), 0)              AS spend,
           coalesce(sum(impressions), 0)        AS impressions,
           coalesce(sum(clicks), 0)             AS clicks,
           coalesce(sum(conversions_lead), 0)   AS leads,
           coalesce(sum(reach), 0)              AS reach,
           avg(search_lost_is_budget)           AS lost_is_budget,
           avg(search_lost_is_rank)             AS lost_is_rank
      FROM metrics_daily
     WHERE entity_ref = :entity_ref AND stat_date >= :start_date AND stat_date < :end_date
""")


class SqlDiagnosisMetricsPort:
    """`optimization.application.ports.DiagnosisMetricsPort`."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._freshness = SqlFreshnessPort(session, clock)
        self._clock = clock

    async def get_metrics(
        self, *, business_id: BusinessId, entity_ref: EntityRef
    ) -> EntityDiagnosisMetrics | None:
        entity_row = await self._entity_row(entity_ref)
        if entity_row is None:
            return None
        as_of = self._clock.now().date()
        recent = await self._window(entity_ref, as_of, _RECENT_WINDOW_DAYS, 0)
        trend_baseline = await self._window(
            entity_ref, as_of, _TREND_BASELINE_DAYS, _RECENT_WINDOW_DAYS
        )
        ratio_baseline = await self._window(
            entity_ref, as_of, _RATIO_BASELINE_DAYS, _RECENT_WINDOW_DAYS
        )
        is_stale = await self._freshness.is_stale(entity_ref)
        delta_hat = await self._latest_delta_hat(business_id, entity_row["platform_account_id"])
        unattributed_share = await self._unattributed_share(business_id, as_of)
        return _build_metrics(
            entity_row=entity_row,
            recent=recent,
            trend_baseline=trend_baseline,
            ratio_baseline=ratio_baseline,
            is_stale=is_stale,
            delta_hat=delta_hat,
            unattributed_share=unattributed_share,
        )

    async def _entity_row(self, entity_ref: EntityRef) -> RowMapping | None:
        result = await self._session.execute(_ENTITY_ROW, {"entity_ref": str(entity_ref)})
        return result.mappings().one_or_none()

    async def _latest_delta_hat(
        self, business_id: BusinessId, platform_account_id: Any
    ) -> float | None:
        result = await self._session.execute(
            _LATEST_DIVERGENCE,
            {"business_id": business_id.value, "platform_account_id": platform_account_id},
        )
        row = result.mappings().one_or_none()
        return None if row is None else float(row["value"])

    async def _unattributed_share(self, business_id: BusinessId, as_of: date) -> float:
        result = await self._session.execute(
            _UNATTRIBUTED_SHARE,
            {
                "business_id": business_id.value,
                "start_date": as_of - timedelta(days=_UNATTRIBUTED_SHARE_WINDOW_DAYS),
                "end_date": as_of + timedelta(days=1),
            },
        )
        share = result.scalar_one_or_none()
        # Sin conversiones en la ventana: nada que atribuir mal todavia --
        # mismo criterio del modulo, no un falso positivo del nodo 1.
        return _NO_MEASUREMENT_GAP_DETECTED if share is None else float(share)

    async def _window(
        self, entity_ref: EntityRef, as_of: date, span_days: int, offset_days: int
    ) -> Mapping[str, Any]:
        end_date = (
            as_of - timedelta(days=offset_days - 1)
            if offset_days
            else as_of + timedelta(days=1)
        )
        start_date = end_date - timedelta(days=span_days)
        result = await self._session.execute(
            _WINDOW_AGGREGATE,
            {"entity_ref": str(entity_ref), "start_date": start_date, "end_date": end_date},
        )
        row = result.mappings().one_or_none()
        return dict(row) if row is not None else _empty_window()


def _empty_window() -> Mapping[str, Any]:
    return {
        "spend": 0,
        "impressions": 0,
        "clicks": 0,
        "leads": 0,
        "reach": 0,
        "lost_is_budget": None,
        "lost_is_rank": None,
    }


def _build_metrics(
    *,
    entity_row: RowMapping,
    recent: Mapping[str, Any],
    trend_baseline: Mapping[str, Any],
    ratio_baseline: Mapping[str, Any],
    is_stale: bool,
    delta_hat: float | None,
    unattributed_share: float,
) -> EntityDiagnosisMetrics:
    status = str(entity_row["status"])
    learning_state = str(entity_row["learning_state"])
    ctr_ratio = _ratio(
        _rate(recent["clicks"], recent["impressions"]),
        _rate(ratio_baseline["clicks"], ratio_baseline["impressions"]),
    )
    click_to_lead_ratio = _ratio(
        _rate(recent["leads"], recent["clicks"]),
        _rate(ratio_baseline["leads"], ratio_baseline["clicks"]),
    )
    cpm_change = _pct_change(
        _cpm(recent["spend"], recent["impressions"]),
        _cpm(trend_baseline["spend"], trend_baseline["impressions"]),
    )
    return EntityDiagnosisMetrics(
        unattributed_share=unattributed_share,
        delta_hat=delta_hat,
        utm_valid=True,
        bridge_has_recent_events_24h=True,
        is_stale=is_stale,
        # `ad_entities.status` (0003_ad_entities) no tiene 'SUSPENDED' --
        # ese vocabulario es de `PlatformAccountStatus`, otro agregado.
        # 'PAUSED'/'REMOVED' son los estados de entidad que corresponden a
        # "no esta comprando" en el sentido que pide el nodo 2 del arbol.
        is_suspended=status in {"PAUSED", "REMOVED"},
        is_drifted=status == "DRIFTED",
        is_learning=learning_state == "LEARNING",
        lost_is_budget_pct=_as_float(recent["lost_is_budget"]),
        lost_is_rank_pct=_as_float(recent["lost_is_rank"]),
        cpm_change_vs_14d_pct=cpm_change,
        ctr_7d_vs_median90d_ratio=ctr_ratio,
        quality_score_below_average=False,
        click_to_lead_vs_median90d_ratio=click_to_lead_ratio,
        lead_to_business_conversion_vs_offering_median_ratio=None,
        frequency=_rate(recent["impressions"], recent["reach"]),
        is_retargeting_audience=False,
        ctr_declining=ctr_ratio is not None and ctr_ratio < 1.0,
        cpm_rising=cpm_change is not None and cpm_change > 0,
        cohort_maturity=None,
        projected_cpe_meets_target=None,
        within_seasonality_band=None,
    )


def _as_float(value: Any) -> float | None:  # noqa: ANN401 - valor crudo de columna SQL
    return None if value is None else float(value)


def _rate(numerator: Any, denominator: Any) -> float | None:  # noqa: ANN401
    denominator_value = float(denominator or 0)
    if denominator_value <= 0:
        return None
    return float(numerator or 0) / denominator_value


def _cpm(spend: Any, impressions: Any) -> float | None:  # noqa: ANN401
    rate = _rate(spend, impressions)
    return None if rate is None else rate * 1000


def _ratio(recent_value: float | None, baseline_value: float | None) -> float | None:
    if recent_value is None or baseline_value is None or baseline_value == 0:
        return None
    return recent_value / baseline_value


def _pct_change(recent_value: float | None, baseline_value: float | None) -> float | None:
    if recent_value is None or baseline_value is None or baseline_value == 0:
        return None
    return (recent_value - baseline_value) / baseline_value
