"""Adaptador SQL de `FreshnessPort` sobre `data_freshness`
(0004_metrics_facts).

`STALE_DATA` de contracts/mcp-tools.md: ninguna decision autonoma se apoya en
metricas viejas. La respuesta se compone de dos fuentes que no se pisan:

1. `is_stale` de la tabla, columna GENERADA (`lag_minutes >
   stale_threshold_minutes`), que es lo que el ciclo de ingesta declara.
2. El reloj: `metrics.domain.Freshness.is_stale`, el mismo servicio de
   dominio de `metrics`, contra `last_ingested_at`. Sin esta segunda
   comprobacion, un ingestor CAIDO se veria eternamente fresco — nadie
   actualizaria `lag_minutes` y la tabla seguiria diciendo que todo va bien.

Sin fila o sin `last_ingested_at`, la respuesta es "viejo": nunca se ingesto
nada de esa entidad, y eso no puede leerse como "los datos estan al dia"."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.metrics.domain.freshness import Freshness
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import EntityLevel, EntityRef

__all__ = ["SqlFreshnessPort"]

# Una fila por (cuenta, nivel de entidad, granularidad): la entidad hereda la
# frescura de su cuenta y su nivel. Si alguna granularidad esta vieja, la
# entidad esta vieja: mezclar una diaria fresca con una horaria parada es
# como no comprobar nada.
_FRESHNESS: Final = """
    SELECT freshness.entity_level, freshness.last_ingested_at,
           freshness.stale_threshold_minutes, freshness.is_stale
      FROM data_freshness AS freshness
      JOIN ad_entities AS entity
        ON entity.platform_account_id = freshness.platform_account_id
       AND entity.level = freshness.entity_level
     WHERE entity.entity_ref = :entity_ref
"""


class SqlFreshnessPort:
    """Implementa `execution.application.ports.FreshnessPort` y, ademas,
    publica `last_ingested_at`: `SqlRuleConditionPort` lo necesita para
    saber si una senal se calculo con los ultimos datos o con los de antes."""

    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def is_stale(self, entity_ref: EntityRef) -> bool:
        rows = await self._rows(entity_ref)
        if not rows:
            return True
        return any(self._row_is_stale(row, entity_ref) for row in rows)

    async def last_ingested_at(self, entity_ref: EntityRef) -> datetime | None:
        """La ingesta mas reciente de la entidad. `None` si nunca hubo."""
        moments = [
            row["last_ingested_at"]
            for row in await self._rows(entity_ref)
            if row["last_ingested_at"] is not None
        ]
        return max(moments) if moments else None

    def _row_is_stale(self, row: RowMapping, entity_ref: EntityRef) -> bool:
        last_ingested_at = row["last_ingested_at"]
        if last_ingested_at is None:
            return True
        if bool(row["is_stale"]):
            return True
        freshness = Freshness(
            platform_account_ref=str(entity_ref),
            entity_level=EntityLevel(str(row["entity_level"])),
            last_ingested_at=last_ingested_at,
        )
        return freshness.is_stale(
            now=self._clock.now(), threshold_minutes=int(row["stale_threshold_minutes"])
        )

    async def _rows(self, entity_ref: EntityRef) -> list[RowMapping]:
        result = await self._session.execute(text(_FRESHNESS), {"entity_ref": str(entity_ref)})
        return list(result.mappings().all())
