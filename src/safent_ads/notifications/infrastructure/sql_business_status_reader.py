"""`SqlBusinessStatusReader` implementa `BusinessStatusPort` (este branch,
`/estado`). Reusa `caps_and_pacing` (`rules`, N4) tal cual -- no hay SQL
nueva para topes/ritmo. El resto (gasto de hoy, señales accionables
abiertas, propuestas pendientes, frescura, freno) repite su propia consulta
minima en vez de importar `panel`/`mcp` (plan.md §4, N7: "ambos leen, nunca
importan" -- ya se aplica entre esos dos hermanos, aqui se extiende al
tercero): son las mismas tablas que `panel.infrastructure.sql_read_model`
ya consulta para su propia vista, con una forma mas pequeña porque
`/estado` no pinta filas por entidad."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import TextClause, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.execution.domain.guardrails import BrakeScope, BrakeScopeKind
from safent_ads.execution.infrastructure.sql_brake_state import SqlBrakeStatePort
from safent_ads.notifications.application.dto import Money
from safent_ads.notifications.application.ports import BusinessStatusView, BusinessSummaryView
from safent_ads.notifications.infrastructure.sql_repositories import SqlSignalsForTicker
from safent_ads.rules.application.read_models.caps_and_pacing import caps_and_pacing
from safent_ads.rules.infrastructure.read_models.caps_and_pacing import SqlAccountRefReadPort
from safent_ads.rules.infrastructure.sql_repositories import SqlGuardrailRepository
from safent_ads.shared.clock import Clock
from safent_ads.shared.ids import BusinessId

_FRESHNESS_STALE_AFTER_MINUTES = 60  # NFR-1, mismo umbral que panel/mcp.

_SELECT_ACTIVE_BUSINESSES = text(
    "SELECT id, name FROM businesses WHERE is_active = true ORDER BY name"
)
_SELECT_BUSINESS_CURRENCY = text(
    "SELECT reference_currency FROM businesses WHERE id = :business_id"
)
_SELECT_SPEND_TODAY = text("""
    SELECT COALESCE(SUM(spend), 0) AS spend_minor
      FROM metrics_daily_physical
     WHERE business_id = :business_id AND entity_level = 'campaign' AND stat_date = :today
""")
_SELECT_SPEND_MTD = text("""
    SELECT COALESCE(SUM(spend), 0) AS spend_minor
      FROM metrics_daily_physical
     WHERE business_id = :business_id AND entity_level = 'campaign'
       AND stat_date BETWEEN :month_start AND :today
""")
_COUNT_PENDING_PROPOSALS = text("""
    SELECT count(*) AS n FROM proposals WHERE business_id = :business_id AND state = 'pending'
""")
_SELECT_FRESHNESS_PER_ACCOUNT = text("""
    SELECT pa.id AS platform_account_id, MAX(m.ingested_at) AS last_ingested_at
      FROM platform_accounts pa
      LEFT JOIN platform_accounts observed ON observed.business_id=pa.business_id
        AND observed.platform=pa.platform AND observed.external_account_id=pa.external_account_id
      LEFT JOIN metrics_daily m ON m.platform_account_id = observed.id
     WHERE pa.business_id = :business_id
     GROUP BY pa.id
""")


class SqlBusinessStatusReader:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def list_businesses(self) -> tuple[BusinessSummaryView, ...]:
        rows = (await self._session.execute(_SELECT_ACTIVE_BUSINESSES)).mappings().all()
        return tuple(
            BusinessSummaryView(business_id=str(row["id"]), name=row["name"]) for row in rows
        )

    async def get_status(self, business_id: BusinessId) -> BusinessStatusView:
        business_uuid = business_id.value
        now = self._clock.now()
        today = now.date()
        month_start = today.replace(day=1)
        currency = await self._currency(business_uuid)

        spend_today_minor = await self._scalar(_SELECT_SPEND_TODAY, business_uuid, today=today)
        spend_mtd_minor = await self._scalar(
            _SELECT_SPEND_MTD, business_uuid, today=today, month_start=month_start
        )
        caps, pacing, is_partial = await caps_and_pacing(
            SqlAccountRefReadPort(self._session),
            SqlGuardrailRepository(self._session),
            business_id=business_uuid,
            spend_mtd_minor=spend_mtd_minor,
            today=today,
            month_start=month_start,
        )
        pending = await self._scalar(_COUNT_PENDING_PROPOSALS, business_uuid)
        signals = await SqlSignalsForTicker(self._session).list_actionable_signals(business_id)
        brake_engaged, brake_mode = await self._brake_state(business_uuid)
        lag_minutes, is_stale = await self._freshness(business_uuid, now=now)

        return BusinessStatusView(
            spend_today=Money(_to_major(spend_today_minor), currency),
            daily_cap=(
                Money(caps.daily.amount, caps.daily.currency) if caps.daily is not None else None
            ),
            pacing_index_pct=pacing.index_pct,
            pacing_projection_pct=pacing.projection_pct,
            open_signals=len(signals),
            pending_proposals=pending,
            brake_engaged=brake_engaged,
            brake_mode=brake_mode,
            freshness_lag_minutes=lag_minutes,
            freshness_is_stale=is_stale,
            is_partial=is_partial,
        )

    async def _currency(self, business_uuid: uuid.UUID) -> str:
        row = (
            await self._session.execute(
                _SELECT_BUSINESS_CURRENCY, {"business_id": business_uuid}
            )
        ).mappings().one()
        return str(row["reference_currency"])

    async def _scalar(self, query: TextClause, business_uuid: uuid.UUID, **extra: date) -> int:
        result = await self._session.execute(query, {"business_id": business_uuid, **extra})
        return int(result.scalar_one())

    async def _brake_state(self, business_uuid: uuid.UUID) -> tuple[bool, str | None]:
        brakes = SqlBrakeStatePort(self._session)
        global_brake = await brakes.get(BrakeScope(kind=BrakeScopeKind.GLOBAL))
        if global_brake is not None and global_brake.engaged:
            return True, global_brake.mode.value
        business_brake = await brakes.get(
            BrakeScope(kind=BrakeScopeKind.BUSINESS, ref=str(business_uuid))
        )
        if business_brake is not None and business_brake.engaged:
            return True, business_brake.mode.value
        return False, None

    async def _freshness(self, business_uuid: uuid.UUID, *, now: datetime) -> tuple[int, bool]:
        rows = (
            await self._session.execute(
                _SELECT_FRESHNESS_PER_ACCOUNT, {"business_id": business_uuid}
            )
        ).mappings().all()
        return _worst_freshness(rows, now=now)


def _to_major(minor: int) -> Decimal:
    return Decimal(minor) / 100


def _worst_freshness(rows: Sequence[RowMapping], *, now: datetime) -> tuple[int, bool]:
    """El peor caso (mayor retraso) entre las cuentas del negocio, mismo
    criterio que `panel`/`mcp`: sin ninguna cuenta o sin metricas
    ingeridas todavia, "un millon de minutos" es la lectura honesta de
    "nunca" (nunca `0`, que leeria como "al dia")."""
    lags = [
        int((now - row["last_ingested_at"]).total_seconds() // 60)
        for row in rows
        if row["last_ingested_at"] is not None
    ]
    if not lags:
        return 1_000_000, True
    worst = max(lags)
    return worst, worst > _FRESHNESS_STALE_AFTER_MINUTES
