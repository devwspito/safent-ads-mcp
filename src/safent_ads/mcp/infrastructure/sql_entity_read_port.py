"""`EntityReadPort` real (integracion, wiring2) sobre `ad_entities`/
`metrics_daily`/`metrics_hourly`/`signals` -- mismas tablas que
`panel.infrastructure.sql_read_model`, proyectadas al DTO propio de `mcp`
(plan.md §4: sin importar `panel`).

Paginacion por conjunto de claves (`entity_ref` es UNIQUE): `cursor` es el
ultimo `entity_ref` devuelto, nunca un OFFSET -- estable aunque se inserten
filas entre paginas.

`list_creatives`/`get_creative` quedan con el mismo comportamiento honesto
que `NotYetWiredEntityReadPort` (lista vacia / `EntityNotFoundError`): el
activo creativo de plataforma (`media_kind`/`format`/`policy_verdict`/
`in_use_by`) no tiene columna en `ad_entities` ni en ninguna tabla de
`creative` todavia (`creative` sigue en repos en memoria, sin migracion
`0011_creative` aterrizada -- ver informe de esta lane). Cablear esto de
verdad es trabajo de `database-engineer`/`backend-engineer`, no de wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.domain.refs import AccountRef
from safent_ads.mcp.application.dto import (
    CampaignStatus,
    CampaignSummary,
    CreativeDetail,
    CreativeSummary,
    EntityLevel,
    EntitySummary,
    Granularity,
    InsightsSnapshot,
    MetricPoint,
    MetricsSeries,
    Page,
    SignalKind,
    Window,
)
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.window_bounds import window_bounds
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.read_models.dto import Money

_MINOR_UNITS_PER_MAJOR = 100
_DEFAULT_LIMIT = 50
# Sin senal emitida todavia: postura neutra, ni compra ni venta (mismo
# significado que `SignalKind.HOLD` en `signals.domain`).
_NO_SIGNAL_KIND = SignalKind.HOLD
# `AdEntityStatus` tiene 5 valores (DRIFTED/LEARNING incluidos); el DTO de
# `mcp` (`CampaignStatus`) solo conoce 3 -- gap preexistente del contrato
# (`mcp/application/dto.py`, fuera del alcance de esta lane). DRIFTED/
# LEARNING se proyectan como ACTIVE: la entidad sigue sirviendo anuncios,
# no esta pausada ni retirada; `learning_state` en `CampaignSummary` ya
# lleva el matiz de LEARNING por separado.
_STATUS_MAP: dict[str, CampaignStatus] = {
    "ACTIVE": CampaignStatus.ACTIVE,
    "PAUSED": CampaignStatus.PAUSED,
    "REMOVED": CampaignStatus.REMOVED,
    "DRIFTED": CampaignStatus.ACTIVE,
    "LEARNING": CampaignStatus.ACTIVE,
}

_SELECT_CAMPAIGNS = text("""
    SELECT e.entity_ref, e.name, e.status, e.is_controllable, e.learning_state,
           e.budget_amount_minor, e.budget_currency, pa.currency, pa.platform
      FROM ad_entities e
      JOIN platform_accounts pa ON pa.id = e.platform_account_id
     WHERE e.business_id = :business_id AND e.level = 'campaign'
       AND (CAST(:account_scope AS TEXT) IS NULL OR pa.account_ref = :account_scope)
       AND (CAST(:platform AS TEXT) IS NULL OR pa.platform = CAST(:platform AS TEXT))
       AND (CAST(:status AS TEXT) IS NULL OR e.status = CAST(:status AS TEXT))
       AND (CAST(:cursor AS TEXT) IS NULL OR e.entity_ref > CAST(:cursor AS TEXT))
     ORDER BY e.entity_ref
     LIMIT :limit
""")

_SELECT_ONE_CAMPAIGN = text("""
    SELECT e.entity_ref, e.name, e.status, e.is_controllable, e.learning_state,
           e.budget_amount_minor, e.budget_currency, pa.currency, pa.platform
      FROM ad_entities e
      JOIN platform_accounts pa ON pa.id = e.platform_account_id
     WHERE e.business_id = :business_id AND e.entity_ref = :entity_ref
       AND (CAST(:account_scope AS TEXT) IS NULL OR pa.account_ref = :account_scope)
""")

_SELECT_LATEST_SIGNAL_KIND = text("""
    SELECT kind FROM signals WHERE entity_ref = :entity_ref ORDER BY emitted_at DESC LIMIT 1
""")

_SELECT_CHILDREN = text("""
    SELECT e.entity_ref, e.name, e.level, e.status, e.is_controllable
      FROM ad_entities e
      JOIN platform_accounts pa ON pa.id = e.platform_account_id
     WHERE e.business_id = :business_id AND e.parent_id = (
               SELECT id FROM ad_entities
                WHERE business_id = :business_id AND entity_ref = :entity_ref
           )
       AND (CAST(:account_scope AS TEXT) IS NULL OR pa.account_ref = :account_scope)
       AND (CAST(:cursor AS TEXT) IS NULL OR e.entity_ref > CAST(:cursor AS TEXT))
     ORDER BY e.entity_ref
     LIMIT :limit
""")

_SELECT_ENTITY_EXISTS = text("""
    SELECT 1 FROM ad_entities e
    JOIN platform_accounts pa ON pa.id = e.platform_account_id
    WHERE e.business_id = :business_id AND e.entity_ref = :entity_ref
      AND (CAST(:account_scope AS TEXT) IS NULL OR pa.account_ref = :account_scope)
""")

_SELECT_METRICS_DAILY = text("""
    SELECT m.stat_date AS period_start, m.currency, SUM(m.spend) AS spend_minor,
           SUM(m.conversions_lead + m.conversions_whatsapp + m.conversions_call
               + m.conversions_business_conversion) AS conversions
      FROM metrics_daily m JOIN platform_accounts pa ON pa.id = m.platform_account_id
     WHERE m.business_id = :business_id AND m.entity_ref = :entity_ref
       AND (CAST(:account_scope AS TEXT) IS NULL OR pa.account_ref = :account_scope)
       AND m.stat_date BETWEEN :start AND :end
     GROUP BY m.stat_date, m.currency
     ORDER BY m.stat_date
""")

_SELECT_METRICS_HOURLY = text("""
    SELECT m.stat_date, m.stat_hour, m.currency, SUM(m.spend) AS spend_minor,
           SUM(m.conversions_lead + m.conversions_whatsapp + m.conversions_call
               + m.conversions_business_conversion) AS conversions
      FROM metrics_hourly m JOIN platform_accounts pa ON pa.id = m.platform_account_id
     WHERE m.business_id = :business_id AND m.entity_ref = :entity_ref
       AND (CAST(:account_scope AS TEXT) IS NULL OR pa.account_ref = :account_scope)
       AND m.stat_date BETWEEN :start AND :end
     GROUP BY m.stat_date, m.stat_hour, m.currency
     ORDER BY m.stat_date, m.stat_hour
""")

_SELECT_BREAKDOWN = text("""
    SELECT COALESCE(SUM(m.conversions_lead), 0) AS lead,
           COALESCE(SUM(m.conversions_whatsapp), 0) AS whatsapp,
           COALESCE(SUM(m.conversions_call), 0) AS call,
           COALESCE(SUM(m.conversions_business_conversion), 0) AS business_conversion
      FROM metrics_daily m JOIN platform_accounts pa ON pa.id = m.platform_account_id
     WHERE m.business_id = :business_id AND m.entity_ref = :entity_ref
       AND (CAST(:account_scope AS TEXT) IS NULL OR pa.account_ref = :account_scope)
       AND m.stat_date BETWEEN :start AND :end
""")


class SqlEntityReadPort:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], clock: Clock | None = None,
        *, account_scope: AccountRef | None = None,
    ) -> None:
        # This is a query boundary, not grant authentication. A managed caller
        # must receive a new instance bound by trusted admission, never mutate a
        # process-global port or choose scope from untrusted tool arguments.
        if account_scope is not None and (
            not isinstance(account_scope.business_id, UUID)
            or not isinstance(account_scope.connection_id, UUID)
        ):
            raise ValueError("Account scope requires business and connection")
        self._session_factory = session_factory
        self._clock = clock or SystemClock()
        self._account_scope = account_scope

    def _scope_parameters(self, business_id: str) -> dict[str, str | None]:
        scope = self._account_scope
        if scope is not None and business_id != str(scope.business_id):
            raise EntityNotFoundError("Cuenta no disponible")
        return {"account_scope": str(scope) if scope is not None else None}

    async def list_campaigns(
        self,
        business_id: str,
        *,
        platform: str | None,
        status: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[CampaignSummary]:
        scope = self._scope_parameters(business_id)
        fetch_limit = min(limit, _DEFAULT_LIMIT) if limit else _DEFAULT_LIMIT
        async with self._session_factory() as session:
            if self._account_scope is not None and cursor is not None:
                exists = (await session.execute(
                    _SELECT_ENTITY_EXISTS,
                    {**scope, "business_id": business_id, "entity_ref": cursor},
                )).scalar_one_or_none()
                if exists is None:
                    raise EntityNotFoundError("Cursor no disponible para esta cuenta")
            rows = (
                await session.execute(
                    _SELECT_CAMPAIGNS,
                    {
                        **scope,
                        "business_id": business_id,
                        "platform": platform,
                        "status": status.upper() if status else None,
                        "cursor": cursor,
                        "limit": fetch_limit + 1,
                    },
                )
            ).mappings().all()
            has_more = len(rows) > fetch_limit
            rows = rows[:fetch_limit]
            items = [await self._campaign(session, row) for row in rows]
        next_cursor = items[-1].entity_ref if has_more and items else None
        return Page(items, next_cursor)

    async def get_campaign(self, business_id: str, entity_ref: str) -> CampaignSummary:
        scope = self._scope_parameters(business_id)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    _SELECT_ONE_CAMPAIGN,
                    {**scope, "business_id": business_id, "entity_ref": entity_ref},
                )
            ).mappings().one_or_none()
            if row is None:
                raise EntityNotFoundError(f"{entity_ref} aun no disponible")
            return await self._campaign(session, row)

    async def _campaign(self, session: AsyncSession, row: RowMapping) -> CampaignSummary:
        signal_kind = (
            await session.execute(_SELECT_LATEST_SIGNAL_KIND, {"entity_ref": row["entity_ref"]})
        ).scalar_one_or_none()
        currency = row["budget_currency"] or row["currency"]
        return CampaignSummary(
            entity_ref=row["entity_ref"],
            name=row["name"],
            status=_STATUS_MAP[row["status"]],
            budget=_money(row["budget_amount_minor"], currency),
            learning_state=row["learning_state"].lower(),
            is_controllable=row["is_controllable"],
            signal_kind=SignalKind(signal_kind.lower()) if signal_kind else _NO_SIGNAL_KIND,
        )

    async def list_children(
        self, business_id: str, entity_ref: str, *, limit: int, cursor: str | None
    ) -> Page[EntitySummary]:
        scope = self._scope_parameters(business_id)
        fetch_limit = min(limit, _DEFAULT_LIMIT) if limit else _DEFAULT_LIMIT
        async with self._session_factory() as session:
            exists = (
                await session.execute(
                    _SELECT_ENTITY_EXISTS,
                    {**scope, "business_id": business_id, "entity_ref": entity_ref},
                )
            ).scalar_one_or_none()
            if exists is None:
                raise EntityNotFoundError(f"{entity_ref} aun no disponible")
            rows = (
                await session.execute(
                    _SELECT_CHILDREN,
                    {
                        **scope,
                        "business_id": business_id,
                        "entity_ref": entity_ref,
                        "cursor": cursor,
                        "limit": fetch_limit + 1,
                    },
                )
            ).mappings().all()
        has_more = len(rows) > fetch_limit
        rows = rows[:fetch_limit]
        items = [
            EntitySummary(
                entity_ref=child["entity_ref"],
                name=child["name"],
                level=EntityLevel(child["level"]),
                status=_STATUS_MAP[child["status"]],
                is_controllable=child["is_controllable"],
            )
            for child in rows
        ]
        next_cursor = items[-1].entity_ref if has_more and items else None
        return Page(items, next_cursor)

    async def list_creatives(
        self, business_id: str, *, media_kind: str | None, limit: int, cursor: str | None
    ) -> Page[CreativeSummary]:
        self._scope_parameters(business_id)
        del business_id, media_kind, limit, cursor
        return Page([], None)

    async def get_creative(self, business_id: str, asset_id: str) -> CreativeDetail:
        self._scope_parameters(business_id)
        del business_id
        raise EntityNotFoundError(f"{asset_id} aun no disponible")

    async def get_entity_metrics(
        self, business_id: str, entity_ref: str, *, window: Window, granularity: str
    ) -> MetricsSeries:
        scope = self._scope_parameters(business_id)
        start, end = window_bounds(window, self._clock.now().date())
        async with self._session_factory() as session:
            exists = (
                await session.execute(
                    _SELECT_ENTITY_EXISTS,
                    {**scope, "business_id": business_id, "entity_ref": entity_ref},
                )
            ).scalar_one_or_none()
            if exists is None:
                raise EntityNotFoundError(f"{entity_ref} aun no disponible")
            if granularity == Granularity.HOURLY.value:
                rows = (
                    await session.execute(
                        _SELECT_METRICS_HOURLY,
                        {**scope, "business_id": business_id,
                         "entity_ref": entity_ref, "start": start, "end": end},
                    )
                ).mappings().all()
                points = [_hourly_point(row) for row in rows]
            else:
                rows = (
                    await session.execute(
                        _SELECT_METRICS_DAILY,
                        {**scope, "business_id": business_id,
                         "entity_ref": entity_ref, "start": start, "end": end},
                    )
                ).mappings().all()
                points = [_daily_point(row) for row in rows]
        return MetricsSeries(entity_ref, Granularity(granularity), points)

    async def get_insights(
        self, business_id: str, entity_ref: str, *, window: Window, breakdown: str | None
    ) -> InsightsSnapshot:
        scope = self._scope_parameters(business_id)
        del breakdown  # una unica proyeccion: mezcla de conversiones (ver docstring)
        start, end = window_bounds(window, self._clock.now().date())
        async with self._session_factory() as session:
            exists = (
                await session.execute(
                    _SELECT_ENTITY_EXISTS,
                    {**scope, "business_id": business_id, "entity_ref": entity_ref},
                )
            ).scalar_one_or_none()
            if exists is None:
                raise EntityNotFoundError(f"{entity_ref} aun no disponible")
            row = (
                await session.execute(
                    _SELECT_BREAKDOWN,
                    {**scope, "business_id": business_id,
                     "entity_ref": entity_ref, "start": start, "end": end},
                )
            ).mappings().one()
        kinds = ("lead", "whatsapp", "call", "business_conversion")
        total = sum(int(row[key]) for key in kinds)
        breakdown_pct = (
            {key: round(int(row[key]) / total * 100, 1) for key in kinds} if total > 0 else {}
        )
        return InsightsSnapshot(
            entity_ref=entity_ref,
            fetched_at=self._clock.now(),
            cached_ttl_seconds=0,
            breakdown=breakdown_pct,
        )

def _to_major(minor: int | Decimal | None) -> Decimal:
    if minor is None:
        return Decimal(0)
    return Decimal(minor) / _MINOR_UNITS_PER_MAJOR


def _money(minor: int | Decimal | None, currency: str) -> Money:
    return Money(_to_major(minor), currency)


def _cost_per_conversion(spend_minor: Decimal, conversions: int, currency: str) -> Money:
    if conversions <= 0:
        return _money(0, currency)
    return _money(round(spend_minor / conversions), currency)


def _daily_point(row: RowMapping) -> MetricPoint:
    conversions = int(row["conversions"])
    spend_minor, currency = row["spend_minor"], row["currency"]
    return MetricPoint(
        period_start=datetime.combine(row["period_start"], datetime.min.time(), UTC),
        spend=_money(spend_minor, currency),
        conversions=conversions,
        cost_per_conversion=_cost_per_conversion(spend_minor, conversions, currency),
    )


def _hourly_point(row: RowMapping) -> MetricPoint:
    conversions = int(row["conversions"])
    spend_minor, currency = row["spend_minor"], row["currency"]
    period_start = datetime.combine(row["stat_date"], datetime.min.time(), UTC).replace(
        hour=int(row["stat_hour"])
    )
    return MetricPoint(
        period_start=period_start,
        spend=_money(spend_minor, currency),
        conversions=conversions,
        cost_per_conversion=_cost_per_conversion(spend_minor, conversions, currency),
    )
