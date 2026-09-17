"""`SqlTopPerformingAdsReadPort` real (R8, historia 24): parte (b) de la
revision previa obligatoria ("anuncios propios con mejor resultado en 30-90
dias"). El filtro por `business_id` y el orden por metrica corren en SQL, no
en Python (`e.business_id = :business_id` en el JOIN, `ORDER BY ... LIMIT`
antes de traer una sola fila): una entidad de otro negocio nunca llega a
materializarse en el resultado."""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp.application.company_read_ports import (
    TopAdsLevel,
    TopAdsMetric,
    TopAdsWindowPreset,
    TopPerformingAd,
    TopPerformingAdMetrics,
    TopPerformingAdsResult,
    top_ads_window_bounds,
)
from safent_ads.shared.clock import Clock

__all__ = ["SqlTopPerformingAdsReadPort"]

_MAX_LIMIT: Final = 25

# Expresion SQL de la metrica de ranking: viene de un `dict` cerrado indexado
# por un `StrEnum` ya validado por pydantic (nunca texto del llamante) --
# igual criterio que `google_ads_adapter._build_select` (constantes propias
# del modulo, no interpolacion de un valor externo).
_METRIC_EXPRESSIONS: Final[dict[TopAdsMetric, str]] = {
    TopAdsMetric.SPEND: "COALESCE(a.cur_spend, 0)",
    TopAdsMetric.CONVERSIONS: "COALESCE(a.cur_conversions, 0)",
    TopAdsMetric.ROAS: (
        "CASE WHEN COALESCE(a.cur_spend, 0) = 0 THEN NULL "
        "ELSE COALESCE(a.cur_conversion_value, 0)::numeric / a.cur_spend END"
    ),
    TopAdsMetric.CPA: (
        "CASE WHEN COALESCE(a.cur_conversions, 0) = 0 THEN NULL "
        "ELSE COALESCE(a.cur_spend, 0)::numeric / a.cur_conversions END"
    ),
    TopAdsMetric.CTR: (
        "CASE WHEN COALESCE(a.cur_impressions, 0) = 0 THEN NULL "
        "ELSE COALESCE(a.cur_clicks, 0)::numeric / a.cur_impressions END"
    ),
}
# CPA es coste: el mejor resultado es el mas BAJO. El resto, el mas ALTO.
_ASCENDING_METRICS: Final = frozenset({TopAdsMetric.CPA})

_CONVERSIONS_EXPR = (
    "m.conversions_lead + m.conversions_whatsapp + m.conversions_call "
    "+ m.conversions_business_conversion"
)

_BASE_QUERY_TEMPLATE = """
    WITH agg AS (
        SELECT
            m.entity_ref,
            MAX(m.currency) AS currency,
            SUM(m.spend) FILTER (WHERE m.stat_date BETWEEN :cur_start AND :cur_end) AS cur_spend,
            SUM(m.impressions)
                FILTER (WHERE m.stat_date BETWEEN :cur_start AND :cur_end) AS cur_impressions,
            SUM(m.clicks)
                FILTER (WHERE m.stat_date BETWEEN :cur_start AND :cur_end) AS cur_clicks,
            SUM({conversions_expr})
                FILTER (WHERE m.stat_date BETWEEN :cur_start AND :cur_end) AS cur_conversions,
            SUM(m.conversion_value)
                FILTER (WHERE m.stat_date BETWEEN :cur_start AND :cur_end) AS cur_conversion_value,
            SUM(m.spend) FILTER (WHERE m.stat_date BETWEEN :prev_start AND :prev_end) AS prev_spend,
            SUM(m.impressions)
                FILTER (WHERE m.stat_date BETWEEN :prev_start AND :prev_end) AS prev_impressions,
            SUM(m.clicks)
                FILTER (WHERE m.stat_date BETWEEN :prev_start AND :prev_end) AS prev_clicks,
            SUM({conversions_expr})
                FILTER (WHERE m.stat_date BETWEEN :prev_start AND :prev_end) AS prev_conversions,
            SUM(m.conversion_value)
                FILTER (WHERE m.stat_date BETWEEN :prev_start AND :prev_end)
                AS prev_conversion_value
        FROM metrics_daily m
        WHERE m.business_id = :business_id
          AND m.stat_date BETWEEN :prev_start AND :cur_end
        GROUP BY m.entity_ref
    )
    SELECT a.entity_ref, e.name, a.currency,
           COALESCE(a.cur_spend, 0) AS cur_spend,
           COALESCE(a.cur_impressions, 0) AS cur_impressions,
           COALESCE(a.cur_clicks, 0) AS cur_clicks,
           COALESCE(a.cur_conversions, 0) AS cur_conversions,
           COALESCE(a.cur_conversion_value, 0) AS cur_conversion_value,
           COALESCE(a.prev_spend, 0) AS prev_spend,
           COALESCE(a.prev_impressions, 0) AS prev_impressions,
           COALESCE(a.prev_clicks, 0) AS prev_clicks,
           COALESCE(a.prev_conversions, 0) AS prev_conversions,
           COALESCE(a.prev_conversion_value, 0) AS prev_conversion_value
      FROM agg a
      JOIN ad_entities e ON e.business_id = :business_id AND e.entity_ref = a.entity_ref
     WHERE e.level = :level
     ORDER BY {ranking_expr} {direction} NULLS LAST
     LIMIT :limit
"""  # noqa: S608 - identificadores fijos del propio modulo, nunca texto del llamante


class SqlTopPerformingAdsReadPort:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def list_top_performing_ads(
        self,
        business_id: str,
        *,
        window: TopAdsWindowPreset,
        level: TopAdsLevel,
        metric: TopAdsMetric,
        limit: int,
    ) -> TopPerformingAdsResult:
        bounded_limit = min(limit, _MAX_LIMIT)
        cur_start, cur_end, prev_start, prev_end = top_ads_window_bounds(
            window, self._clock.now().date()
        )
        query = text(
            _BASE_QUERY_TEMPLATE.format(
                conversions_expr=_CONVERSIONS_EXPR,
                ranking_expr=_METRIC_EXPRESSIONS[metric],
                direction="ASC" if metric in _ASCENDING_METRICS else "DESC",
            )
        )
        params = {
            "business_id": business_id,
            "level": level.value,
            "cur_start": cur_start,
            "cur_end": cur_end,
            "prev_start": prev_start,
            "prev_end": prev_end,
            "limit": bounded_limit,
        }
        async with self._session_factory() as session:
            rows = (await session.execute(query, params)).mappings().all()
        ads = tuple(_row_to_ad(row, level, metric) for row in rows)
        return TopPerformingAdsResult(
            business_id=business_id, window_preset=window, level=level, metric=metric, ads=ads
        )


def _row_to_ad(row: Any, level: TopAdsLevel, metric: TopAdsMetric) -> TopPerformingAd:
    current = _metrics_from_row(row, prefix="cur_")
    previous = _metrics_from_row(row, prefix="prev_")
    return TopPerformingAd(
        entity_ref=str(row["entity_ref"]),
        name=str(row["name"]),
        level=level,
        currency=str(row["currency"] or "EUR"),
        current=current,
        previous=previous,
        delta_metric_pct=_delta_pct(
            _metric_value(current, metric), _metric_value(previous, metric)
        ),
    )


_METRIC_ACCESSORS: Final[dict[TopAdsMetric, Any]] = {
    TopAdsMetric.SPEND: lambda m: m.spend_minor,
    TopAdsMetric.CONVERSIONS: lambda m: m.conversions,
    TopAdsMetric.ROAS: lambda m: m.roas,
    TopAdsMetric.CPA: lambda m: m.cpa_minor,
    TopAdsMetric.CTR: lambda m: m.ctr,
}


def _metric_value(metrics: TopPerformingAdMetrics, metric: TopAdsMetric) -> float | None:
    value = _METRIC_ACCESSORS[metric](metrics)
    return None if value is None else float(value)


def _metrics_from_row(row: Any, *, prefix: str) -> TopPerformingAdMetrics:
    spend_minor = int(row[f"{prefix}spend"])
    conversions = int(row[f"{prefix}conversions"])
    impressions = int(row[f"{prefix}impressions"])
    clicks = int(row[f"{prefix}clicks"])
    conversion_value_minor = int(row[f"{prefix}conversion_value"])
    return TopPerformingAdMetrics(
        spend_minor=spend_minor,
        conversions=conversions,
        cpa_minor=round(spend_minor / conversions) if conversions > 0 else None,
        roas=(conversion_value_minor / spend_minor) if spend_minor > 0 else None,
        ctr=(clicks / impressions) if impressions > 0 else None,
    )


def _delta_pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous) * 100
