"""Repositorios SQL de `metrics` sobre `metrics_daily` (particionada por
mes), `metrics_hourly` y `metrics_restatements` (migracion 0004).

SQL de mano con parametros ligados; ninguna clase de SQLAlchemy sale de
aqui. Los repositorios hacen `flush`, nunca `commit`: la transaccion es de
`application` (plan.md §8).

Decisiones de mapeo, que son decisiones y no obviedad:

- `MetricFact` no lleva `business_id`, `platform_account_id` ni
  `entity_level`: el esquema los exige porque `metrics_daily` se consulta
  por negocio y por cuenta. Se resuelven en la propia sentencia desde
  `ad_entities`, que es quien los posee (`accounts` es N1, `metrics` N2). Un
  hecho de una entidad desconocida no se inserta: se rechaza.
- `stat_hour IS NULL` decide la tabla: diaria u horaria. Es la misma clave
  natural del dominio `(entity_ref, stat_date, stat_hour)`.
- `search_lost_is_*_pct` viaja en puntos porcentuales (0-100) en el dominio
  y se guarda como cuota 0-1, que es como lo reporta Google y como lo
  restringe el CHECK del esquema.
- Un contador a cero y un contador ausente son indistinguibles en el
  esquema (columnas NOT NULL DEFAULT 0): al leer solo se reconstruyen los
  tipos de conversion con valor distinto de cero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.metrics.domain.conversion_kind import ConversionKind
from safent_ads.metrics.domain.freshness import Freshness
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.domain.restatement import Restatement
from safent_ads.metrics.infrastructure.errors import UnknownAccountRefError, UnknownEntityRefError
from safent_ads.shared.ids import EntityRef

__all__ = ["SqlFreshnessRepository", "SqlMetricFactRepository", "SqlRestatementRepository"]

_PERCENT: Final = 100

_UPSERT_DAILY: Final = """
    INSERT INTO metrics_daily (business_id, entity_ref, entity_level, platform_account_id,
                               stat_date, account_timezone, currency, spend, impressions,
                               clicks, reach, conversions_lead, conversions_whatsapp,
                               conversions_call, conversions_business_conversion, conversion_value,
                               video_views_3s, video_views_75pct, search_lost_is_budget,
                               search_lost_is_rank, revision, ingested_at)
    SELECT entity.business_id, :entity_ref, entity.level, entity.platform_account_id,
           :stat_date, :account_timezone, :currency, :spend, :impressions, :clicks, :reach,
           :conversions_lead, :conversions_whatsapp, :conversions_call,
           :conversions_business_conversion, :conversion_value, :video_views_3s, :video_views_75pct,
           :search_lost_is_budget, :search_lost_is_rank, :revision, :ingested_at
      FROM ad_entities AS entity
     WHERE entity.entity_ref = :entity_ref
    ON CONFLICT (entity_ref, stat_date) DO UPDATE
        SET account_timezone      = EXCLUDED.account_timezone,
            currency              = EXCLUDED.currency,
            spend                 = EXCLUDED.spend,
            impressions           = EXCLUDED.impressions,
            clicks                = EXCLUDED.clicks,
            reach                 = EXCLUDED.reach,
            conversions_lead      = EXCLUDED.conversions_lead,
            conversions_whatsapp  = EXCLUDED.conversions_whatsapp,
            conversions_call      = EXCLUDED.conversions_call,
            conversions_business_conversion = EXCLUDED.conversions_business_conversion,
            conversion_value      = EXCLUDED.conversion_value,
            video_views_3s        = EXCLUDED.video_views_3s,
            video_views_75pct     = EXCLUDED.video_views_75pct,
            search_lost_is_budget = EXCLUDED.search_lost_is_budget,
            search_lost_is_rank   = EXCLUDED.search_lost_is_rank,
            revision              = EXCLUDED.revision,
            ingested_at           = EXCLUDED.ingested_at
    WHERE (EXCLUDED.ingested_at, EXCLUDED.revision) >=
          (metrics_daily.ingested_at, metrics_daily.revision)
    RETURNING entity_ref
"""

_UPSERT_HOURLY: Final = """
    INSERT INTO metrics_hourly (business_id, entity_ref, platform_account_id, stat_date,
                                stat_hour, account_timezone, currency, spend, impressions,
                                clicks, reach, conversions_lead, conversions_whatsapp,
                                conversions_call, conversions_business_conversion, conversion_value,
                                video_views_3s, video_views_75pct, search_lost_is_budget,
                                search_lost_is_rank, revision, ingested_at)
    SELECT entity.business_id, :entity_ref, entity.platform_account_id, :stat_date,
           :stat_hour, :account_timezone, :currency, :spend, :impressions, :clicks, :reach,
           :conversions_lead, :conversions_whatsapp, :conversions_call,
           :conversions_business_conversion, :conversion_value, :video_views_3s, :video_views_75pct,
           :search_lost_is_budget, :search_lost_is_rank, :revision, :ingested_at
      FROM ad_entities AS entity
     WHERE entity.entity_ref = :entity_ref
    ON CONFLICT (entity_ref, stat_date, stat_hour) DO UPDATE
        SET account_timezone      = EXCLUDED.account_timezone,
            currency              = EXCLUDED.currency,
            spend                 = EXCLUDED.spend,
            impressions           = EXCLUDED.impressions,
            clicks                = EXCLUDED.clicks,
            conversions_lead      = EXCLUDED.conversions_lead,
            conversions_whatsapp  = EXCLUDED.conversions_whatsapp,
            conversions_call      = EXCLUDED.conversions_call,
            conversions_business_conversion = EXCLUDED.conversions_business_conversion,
            conversion_value      = EXCLUDED.conversion_value,
            revision              = EXCLUDED.revision,
            ingested_at           = EXCLUDED.ingested_at
    WHERE (EXCLUDED.ingested_at, EXCLUDED.revision) >=
          (metrics_hourly.ingested_at, metrics_hourly.revision)
    RETURNING entity_ref
"""

# Las dos tablas se leen con la misma forma de fila para poder unirlas. SQL
# literal, sin componer cadenas (ruff S608).
_SELECT_DAILY: Final = """
    SELECT entity_ref, stat_date, NULL::smallint AS stat_hour, account_timezone, currency,
           spend, impressions, clicks, reach, conversions_lead, conversions_whatsapp,
           conversions_call, conversions_business_conversion, conversion_value, video_views_3s,
           video_views_75pct, search_lost_is_budget, search_lost_is_rank, revision,
           ingested_at
      FROM metrics_daily
     WHERE entity_ref = :entity_ref
"""

_SELECT_HOURLY: Final = """
    SELECT entity_ref, stat_date, stat_hour, account_timezone, currency,
           spend, impressions, clicks, reach, conversions_lead,
           conversions_whatsapp, conversions_call, conversions_business_conversion,
           conversion_value, video_views_3s, video_views_75pct, search_lost_is_budget,
           search_lost_is_rank, revision, ingested_at
      FROM metrics_hourly
     WHERE entity_ref = :entity_ref
"""

# `data_freshness` no lleva UNIQUE por `entity_ref` (es por cuenta+nivel, no
# por entidad): se resuelve el `platform_account_id` desde `platform_accounts`
# por su clave natural (platform, external_account_id), igual que las otras
# UPSERT de este archivo resuelven `ad_entities` por `entity_ref`.
# `lag_minutes` se escribe en 0 a proposito: `SqlFreshnessPort.is_stale`
# (execution/infrastructure/sql_freshness.py) recalcula la frescura real
# contra el reloj y `last_ingested_at` en cada lectura, asi que esta columna
# nunca es la fuente de verdad -- solo un valor de partida coherente con el
# DEFAULT del esquema (0004_metrics_facts.py).
_UPSERT_FRESHNESS: Final = """
    INSERT INTO data_freshness (platform_account_id, entity_level, granularity,
                                last_ingested_at, lag_minutes)
    SELECT id, :entity_level, 'daily', :last_ingested_at, 0
      FROM platform_accounts
     WHERE account_ref = :account_ref
    ON CONFLICT (platform_account_id, entity_level, granularity) DO UPDATE
        SET last_ingested_at = EXCLUDED.last_ingested_at,
            lag_minutes      = EXCLUDED.lag_minutes
    RETURNING platform_account_id
"""

_FIND_DAILY: Final = f"{_SELECT_DAILY} AND stat_date = :day"
_FIND_HOURLY: Final = f"{_SELECT_HOURLY} AND stat_date = :day AND stat_hour = :hour"
_FIND_WINDOW: Final = (
    f"{_SELECT_DAILY} AND stat_date BETWEEN :start AND :end "
    f"UNION ALL {_SELECT_HOURLY} AND stat_date BETWEEN :start AND :end "
    "ORDER BY stat_date, stat_hour NULLS FIRST"
)


class SqlMetricFactRepository:
    """`MetricFactRepository` (metrics/application/ports.py)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(self, facts: Sequence[MetricFact]) -> None:
        for fact in facts:
            statement = _UPSERT_HOURLY if fact.is_hourly else _UPSERT_DAILY
            result = await self._session.execute(text(statement), _fact_params(fact))
            if result.first() is None:
                known = await self._session.execute(
                    text("SELECT 1 FROM ad_entities WHERE entity_ref=:ref"),
                    {"ref": str(fact.entity_ref)},
                )
                if known.scalar_one_or_none() is not None:
                    continue  # out-of-order retry, not a new provider observation
                raise UnknownEntityRefError(
                    f"{fact.entity_ref} no existe en ad_entities: no se ingiere su metrica"
                )
        await self._session.flush()

    async def find_by_natural_key(
        self, *, entity_ref: EntityRef, stat_date: date, stat_hour: int | None
    ) -> MetricFact | None:
        if stat_hour is None:
            statement = _FIND_DAILY
            params: Mapping[str, Any] = {"entity_ref": str(entity_ref), "day": stat_date}
        else:
            statement = _FIND_HOURLY
            params = {"entity_ref": str(entity_ref), "day": stat_date, "hour": stat_hour}
        row = (await self._session.execute(text(statement), params)).mappings().one_or_none()
        return None if row is None else _to_fact(row)

    async def find_in_window(
        self, *, entity_ref: EntityRef, start_date: date, end_date: date
    ) -> Sequence[MetricFact]:
        result = await self._session.execute(
            text(_FIND_WINDOW),
            {"entity_ref": str(entity_ref), "start": start_date, "end": end_date},
        )
        return [_to_fact(row) for row in result.mappings()]

    async def latest_ingested_at(self, *, entity_ref: EntityRef) -> datetime | None:
        result = await self._session.execute(
            text(
                """
                SELECT max(ingested_at) AS latest FROM (
                    SELECT ingested_at FROM metrics_daily WHERE entity_ref = :entity_ref
                    UNION ALL
                    SELECT ingested_at FROM metrics_hourly WHERE entity_ref = :entity_ref
                ) AS ingestions
                """
            ),
            {"entity_ref": str(entity_ref)},
        )
        return result.scalar_one_or_none()


class SqlRestatementRepository:
    """`RestatementRepository`: solo-anexable, con triggers que rechazan
    UPDATE y DELETE (migracion 0004). `source` no viaja en el dominio; el
    adaptador declara de donde viene la correccion."""

    def __init__(self, session: AsyncSession, *, source: str = "platform") -> None:
        self._session = session
        self._source = source

    async def record(self, restatement: Restatement) -> None:
        result = await self._session.execute(
            text(
                """
                INSERT INTO metrics_restatements (business_id, entity_ref, stat_date, stat_hour,
                                                  metric, old_value, new_value, reason, source,
                                                  revision, restated_at)
                SELECT entity.business_id, :entity_ref, :stat_date, :stat_hour, :metric,
                       :old_value, :new_value, :reason, :source, 1, :restated_at
                  FROM ad_entities AS entity
                 WHERE entity.entity_ref = :entity_ref
                RETURNING id
                """
            ),
            {
                "entity_ref": str(restatement.entity_ref),
                "stat_date": restatement.stat_date,
                "stat_hour": restatement.stat_hour,
                "metric": restatement.field_name,
                "old_value": restatement.old_value,
                "new_value": restatement.new_value,
                "reason": restatement.reason,
                "source": self._source,
                "restated_at": restatement.recorded_at,
            },
        )
        if result.first() is None:
            raise UnknownEntityRefError(
                f"{restatement.entity_ref} no existe en ad_entities: no se anota su correccion"
            )
        await self._session.flush()

    async def list_for_entity(self, *, entity_ref: EntityRef) -> Sequence[Restatement]:
        result = await self._session.execute(
            text(
                """
                SELECT entity_ref, stat_date, stat_hour, metric, old_value, new_value,
                       reason, restated_at
                  FROM metrics_restatements
                 WHERE entity_ref = :entity_ref
                 ORDER BY id
                """
            ),
            {"entity_ref": str(entity_ref)},
        )
        return [
            Restatement(
                entity_ref=EntityRef.parse(row["entity_ref"]),
                stat_date=row["stat_date"],
                stat_hour=row["stat_hour"],
                field_name=row["metric"],
                old_value=int(row["old_value"]),
                new_value=int(row["new_value"]),
                reason=row["reason"],
                recorded_at=row["restated_at"],
            )
            for row in result.mappings()
        ]


class SqlFreshnessRepository:
    """`FreshnessRepository` (metrics/application/ports.py) sobre
    `data_freshness` (0004_metrics_facts): la contraparte de escritura de
    `execution.infrastructure.sql_freshness.SqlFreshnessPort`, que solo lee.

    `metrics` no importa `accounts` (mismo criterio que `signals`, que
    define su propio `LearningStatus` en vez de reusar
    `accounts.domain.learning_state.LearningState`): `platform_account_ref`
    se trocea a mano en vez de reconstruir `accounts.domain.refs.AccountRef`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, freshness: Freshness) -> None:
        result = await self._session.execute(
            text(_UPSERT_FRESHNESS),
            {
                "account_ref": freshness.platform_account_ref,
                "entity_level": freshness.entity_level.value,
                "last_ingested_at": freshness.last_ingested_at,
            },
        )
        if result.first() is None:
            raise UnknownAccountRefError(
                f"{freshness.platform_account_ref} no existe en platform_accounts: "
                "no se guarda su frescura"
            )
        await self._session.flush()


def _fact_params(fact: MetricFact) -> Mapping[str, Any]:
    return {
        "entity_ref": str(fact.entity_ref),
        "stat_date": fact.stat_date,
        "stat_hour": fact.stat_hour,
        "account_timezone": fact.account_timezone,
        "currency": fact.currency,
        "spend": fact.spend_minor,
        "impressions": fact.impressions,
        "clicks": fact.clicks,
        "reach": fact.reach,
        "conversions_lead": fact.conversions_of(ConversionKind.LEAD),
        "conversions_whatsapp": fact.conversions_of(ConversionKind.WHATSAPP),
        "conversions_call": fact.conversions_of(ConversionKind.CALL),
        "conversions_business_conversion": fact.conversions_of(ConversionKind.BUSINESS_CONVERSION),
        "conversion_value": fact.conversion_value_minor,
        "video_views_3s": fact.video_views_3s,
        "video_views_75pct": fact.video_views_75pct,
        "search_lost_is_budget": _to_share(fact.search_lost_is_budget_pct),
        "search_lost_is_rank": _to_share(fact.search_lost_is_rank_pct),
        "revision": fact.revision,
        "ingested_at": fact.ingested_at,
    }


def _to_share(percentage_points: float | None) -> float | None:
    return None if percentage_points is None else percentage_points / _PERCENT


def _to_percentage_points(share: Decimal | None) -> float | None:
    """La multiplicacion va en `Decimal` a proposito: `float(Decimal("0.23"))
    * 100` da 23.000000000000004 y eso convierte una comparacion exacta en
    una loteria."""
    return None if share is None else float(share * _PERCENT)


def _to_fact(row: RowMapping) -> MetricFact:
    conversions = {
        kind: int(row[f"conversions_{kind.value}"])
        for kind in ConversionKind
        if int(row[f"conversions_{kind.value}"]) != 0
    }
    return MetricFact(
        entity_ref=EntityRef.parse(row["entity_ref"]),
        stat_date=row["stat_date"],
        stat_hour=row["stat_hour"],
        account_timezone=row["account_timezone"],
        currency=row["currency"],
        spend_minor=int(row["spend"]),
        impressions=int(row["impressions"]),
        clicks=int(row["clicks"]),
        reach=int(row["reach"] or 0),
        conversions=MappingProxyType(conversions),
        conversion_value_minor=int(row["conversion_value"]),
        video_views_3s=int(row["video_views_3s"] or 0),
        video_views_75pct=int(row["video_views_75pct"] or 0),
        search_lost_is_budget_pct=_to_percentage_points(row["search_lost_is_budget"]),
        search_lost_is_rank_pct=_to_percentage_points(row["search_lost_is_rank"]),
        ingested_at=row["ingested_at"],
        revision=int(row["revision"]),
    )
