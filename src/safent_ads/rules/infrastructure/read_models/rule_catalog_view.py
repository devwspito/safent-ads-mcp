"""Adaptador SQL de `rules.application.read_models.rule_catalog_view` (I-1,
revision final T130): el conteo de disparos de los ultimos 30 dias
(`rule_firings`) vive aqui, detras de `ports.RuleActivityReadPort`. El
resto de la proyeccion (`RuleView`, `list_rule_views`, `stored_rule_to_view`)
sigue siendo pura en `application/read_models/rule_catalog_view.py`."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.rules.application.read_models.rule_catalog_view import RuleActivity

__all__ = ["SqlRuleActivityReadPort"]

_RULE_ACTIVITY_30D: Final = text("""
    SELECT rule.code AS code,
           count(*) AS total,
           count(*) FILTER (WHERE firing.outcome IN ('PROPOSED', 'AUTHORIZED')) AS successful
      FROM rule_firings AS firing
      JOIN rules AS rule ON rule.id = firing.rule_id
     WHERE firing.business_id = :business_id
       AND firing.fired_at >= :since
     GROUP BY rule.code
""")


class SqlRuleActivityReadPort:
    """Implementa `rules.application.read_models.ports.RuleActivityReadPort`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def activity_by_code(
        self, *, business_id: str, since: datetime
    ) -> dict[str, RuleActivity]:
        rows = (
            await self._session.execute(
                _RULE_ACTIVITY_30D, {"business_id": business_id, "since": since}
            )
        ).mappings()
        return {
            row["code"]: RuleActivity(total=row["total"], successful=row["successful"])
            for row in rows
        }
