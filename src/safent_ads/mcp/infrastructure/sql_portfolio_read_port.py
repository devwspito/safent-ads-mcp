"""`PortfolioReadPort` real (integracion, wiring2) sobre `platform_accounts`/
`ad_entities`/`metrics_daily`/`guardrails` -- mismas tablas que
`panel.infrastructure.sql_read_model.SqlPanelReadPort.get_portfolio`, pero
proyectadas al `PortfolioOverview` propio de `mcp` (plan.md §4: `mcp` no
importa `panel`).

Reutiliza lo que ya es del contexto correcto y no es solo SQL de proyeccion:
`SqlAccountRepository.list_by_business` (`accounts`, agregado real) y
`SqlGuardrailRepository.find_for_account` (`rules`). El resto -- gasto por
ventana, series para "top movers", frescura -- son proyecciones de lectura
propias, igual que las de `panel`: mismo principio (CQRS por contexto), SQL
distinto porque el DTO de salida es distinto.

Limitacion heredada de `panel` (misma razon, documentada alli): los topes/
ritmo de cartera se leen de la PRIMERA cuenta publicitaria del negocio --
combinar guardarrailes de varias cuentas en un unico tope de cartera no
esta definido en ningun contrato todavia."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.accounts.infrastructure.sql_repositories import SqlAccountRepository
from safent_ads.mcp.application.dto import (
    AccountFreshness,
    AccountStatus,
    DegradedAccountSummary,
    PlatformAccountSummary,
    PlatformCode,
    PortfolioOverview,
    TopMover,
    Window,
)
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.infrastructure.window_bounds import window_bounds
from safent_ads.rules.application.read_models.caps_and_pacing import caps_and_pacing
from safent_ads.rules.application.read_models.caps_and_pacing import money as _money
from safent_ads.rules.application.read_models.caps_and_pacing import to_major as _to_major
from safent_ads.rules.infrastructure.read_models.caps_and_pacing import SqlAccountRefReadPort
from safent_ads.rules.infrastructure.read_models.caps_and_pacing import (
    spend_in_range_minor as _spend_in_range_minor,
)
from safent_ads.rules.infrastructure.sql_repositories import SqlGuardrailRepository
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.ids import BusinessId
from safent_ads.shared.read_models.dto import Freshness, Money, SpendBreakdown

_FRESHNESS_STALE_AFTER_MINUTES = 60
_TOP_MOVERS_LIMIT = 3

_SELECT_ENTITIES_FOR_BUSINESS = text("""
    SELECT e.entity_ref, e.name, pa.id AS platform_account_id, pa.platform,
           pa.external_account_id, pa.account_ref, pa.currency, pa.status AS account_status
      FROM ad_entities_physical e
      JOIN platform_accounts pa ON pa.id = e.platform_account_id
     WHERE e.business_id = :business_id AND e.level = 'campaign'
""")

_SELECT_SPEND_IN_WINDOW = text("""
    SELECT physical_entity_ref AS entity_ref, COALESCE(SUM(spend), 0) AS spend_minor,
           COALESCE(SUM(conversions_lead), 0) AS conversions_lead,
           COALESCE(SUM(conversions_whatsapp), 0) AS conversions_whatsapp,
           COALESCE(SUM(conversions_call), 0) AS conversions_call,
           COALESCE(SUM(conversions_business_conversion), 0) AS conversions_business_conversion
      FROM metrics_daily_physical
     WHERE business_id = :business_id AND entity_level = 'campaign'
       AND stat_date BETWEEN :start AND :end
     GROUP BY physical_entity_ref
""")

_SELECT_FRESHNESS_PER_ACCOUNT = text("""
    SELECT pa.platform, pa.external_account_id, pa.account_ref,
           MAX(m.ingested_at) AS last_ingested_at
      FROM platform_accounts pa
      LEFT JOIN platform_accounts observed ON observed.business_id=pa.business_id
        AND observed.platform=pa.platform AND observed.external_account_id=pa.external_account_id
      LEFT JOIN metrics_daily m ON m.platform_account_id = observed.id
     WHERE pa.business_id = :business_id
     GROUP BY pa.platform, pa.external_account_id, pa.account_ref
""")


class SqlPortfolioReadPort:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], clock: Clock | None = None
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    async def list_platform_accounts(self, business_id: str) -> list[PlatformAccountSummary]:
        async with self._session_factory() as session:
            accounts = await SqlAccountRepository(session).list_by_business(
                BusinessId.parse(business_id)
            )
        return [
            PlatformAccountSummary(
                account_ref=str(account.account_ref),
                platform=PlatformCode(account.account_ref.platform.value),
                currency=account.currency,
                timezone=account.timezone,
                status=AccountStatus(account.status.value),
                api_tier=account.api_tier.value,
            )
            for account in accounts
        ]

    async def get_data_freshness(self, business_id: str) -> list[AccountFreshness]:
        async with self._session_factory() as session:
            rows = (
                (await session.execute(_SELECT_FRESHNESS_PER_ACCOUNT, {"business_id": business_id}))
                .mappings()
                .all()
            )
        now = self._clock.now()
        return [
            AccountFreshness(
                account_ref=row["account_ref"],
                freshness=_freshness(row["last_ingested_at"], now),
            )
            for row in rows
        ]

    async def get_portfolio_overview(self, business_id: str, window: Window) -> PortfolioOverview:
        today = self._clock.now().date()
        start, end = window_bounds(window, today)
        month_start = today.replace(day=1)

        async with self._session_factory() as session:
            entities = (
                (await session.execute(_SELECT_ENTITIES_FOR_BUSINESS, {"business_id": business_id}))
                .mappings()
                .all()
            )
            if not entities:
                raise EntityNotFoundError(f"cartera aun no disponible para {business_id}")

            spend_rows = {
                row["entity_ref"]: row
                for row in (
                    await session.execute(
                        _SELECT_SPEND_IN_WINDOW,
                        {"business_id": business_id, "start": start, "end": end},
                    )
                ).mappings()
            }
            spend_today_minor = await _spend_in_range_minor(
                session, business_id=business_id, start=today, end=today
            )
            spend_mtd_minor = await _spend_in_range_minor(
                session, business_id=business_id, start=month_start, end=today
            )
            caps, pacing, is_partial = await caps_and_pacing(
                SqlAccountRefReadPort(session),
                SqlGuardrailRepository(session),
                business_id=business_id,
                spend_mtd_minor=spend_mtd_minor,
                today=today,
                month_start=month_start,
            )
            freshness_rows = (
                (await session.execute(_SELECT_FRESHNESS_PER_ACCOUNT, {"business_id": business_id}))
                .mappings()
                .all()
            )
            top_movers = await self._top_movers(session, business_id, entities, start, end)

        currency = entities[0]["currency"]
        window_spend_minor = sum(int(row["spend_minor"]) for row in spend_rows.values())
        conversions_by_kind = {
            "lead": sum(int(row["conversions_lead"]) for row in spend_rows.values()),
            "whatsapp": sum(int(row["conversions_whatsapp"]) for row in spend_rows.values()),
            "call": sum(int(row["conversions_call"]) for row in spend_rows.values()),
            "business_conversion": sum(
                int(row["conversions_business_conversion"]) for row in spend_rows.values()
            ),
        }
        now = self._clock.now()
        degraded = [
            DegradedAccountSummary(
                platform_account_id=row["account_ref"],
                platform=PlatformCode(row["platform"]),
                status=AccountStatus(row["account_status"].lower()),
                reason="platform_account_status_not_active",
            )
            for row in entities
            if row["account_status"] != "ACTIVE"
        ]
        # Una entrada por cuenta degradada, no una por campana.
        degraded = list({item.platform_account_id: item for item in degraded}.values())

        return PortfolioOverview(
            spend=SpendBreakdown(
                window=_money(window_spend_minor, currency),
                today=_money(spend_today_minor, currency),
                mtd=_money(spend_mtd_minor, currency),
            ),
            caps=caps,
            pacing=pacing,
            conversions_by_kind=conversions_by_kind,
            cost_per_lead=_cost_per(window_spend_minor, conversions_by_kind["lead"], currency),
            cost_per_business_conversion=_cost_per(
                window_spend_minor, conversions_by_kind["business_conversion"], currency
            ),
            freshness=_business_freshness(freshness_rows, now),
            is_partial=is_partial,
            degraded_accounts=degraded,
            top_movers=top_movers,
        )

    async def _top_movers(
        self,
        session: AsyncSession,
        business_id: str,
        entities: Sequence[RowMapping],
        start: date,
        end: date,
    ) -> list[TopMover]:
        span = (end - start).days + 1
        previous_end = start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=span - 1)
        current_rows = (
            await session.execute(
                _SELECT_SPEND_IN_WINDOW, {"business_id": business_id, "start": start, "end": end}
            )
        ).mappings()
        previous_rows = (
            await session.execute(
                _SELECT_SPEND_IN_WINDOW,
                {"business_id": business_id, "start": previous_start, "end": previous_end},
            )
        ).mappings()
        current_by_entity = {row["entity_ref"]: int(row["spend_minor"]) for row in current_rows}
        previous_by_entity = {row["entity_ref"]: int(row["spend_minor"]) for row in previous_rows}
        names = {row["entity_ref"]: row["name"] for row in entities}

        movers: list[TopMover] = []
        for entity_ref, previous_minor in previous_by_entity.items():
            if previous_minor <= 0 or entity_ref not in names:
                continue
            current_minor = current_by_entity.get(entity_ref, 0)
            delta_pct = (current_minor - previous_minor) / previous_minor * 100
            movers.append(TopMover(entity_ref, names[entity_ref], round(delta_pct, 1), "spend"))
        movers.sort(key=lambda mover: abs(mover.delta_pct), reverse=True)
        return movers[:_TOP_MOVERS_LIMIT]


def _cost_per(spend_minor: int, conversions: int, currency: str) -> Money | None:
    if conversions <= 0:
        return None
    return Money(_to_major(round(spend_minor / conversions)), currency)


def _freshness(last_ingested_at: datetime | None, now: datetime) -> Freshness:
    if last_ingested_at is None:
        return Freshness(last_ingested_at=now, lag_minutes=1_000_000, is_stale=True)
    lag_minutes = int((now - last_ingested_at).total_seconds() // 60)
    return Freshness(
        last_ingested_at=last_ingested_at,
        lag_minutes=lag_minutes,
        is_stale=lag_minutes > _FRESHNESS_STALE_AFTER_MINUTES,
    )


def _business_freshness(rows: Sequence[RowMapping], now: datetime) -> Freshness:
    if not rows:
        return Freshness(last_ingested_at=now, lag_minutes=1_000_000, is_stale=True)
    per_account = [_freshness(row["last_ingested_at"], now) for row in rows]
    return max(per_account, key=lambda item: item.lag_minutes)
