"""`SqlPanelReadPort` (integracion): implementa `PanelReadPort` con
consultas SQL directas sobre `platform_accounts`/`ad_entities`/
`metrics_daily`/`signals`/`anomalies`/`guardrails`/`emergency_brakes`/
`proposals` -- sin fuga de ORM, sin logica de negocio nueva mas alla de
recomponer lecturas (`signals.domain.pacing` hace la aritmetica de ritmo,
esto solo la alimenta con datos reales).

Dos simplificaciones deliberadas, documentadas para quien las levante
despues:

1. `CreativeBadges.pending_approval` queda en 0: `creative` sigue sobre
   repos en memoria propios (`creative/infrastructure/
   in_memory_repositories.py`, sin adaptador SQL mergeado) -- no hay fila
   real que contar todavia. Contar 0 en vez de fallar es la lectura
   honesta: cero activos creativos reales existen en este proceso.
   `pending_proposals`/`deferred_proposals`/`ProposalBadges` SI cuentan
   filas reales (`proposals`, 0008_proposals, `us2-sqlrepos`) desde que
   `SqlProposalRepository` aterrizo -- ver `_COUNT_PROPOSAL_BADGES`:
   `pending`=`state='pending'`, `critical`=subconjunto con
   `urgency='critical'` (data-model.md `Urgency`: 24h de gracia, la unica
   lectura que justifica una insignia aparte de "pendiente"),
   `deferred`=`state='postponed'`. Ni `approved`/`scheduled` ni los
   estados resueltos cuentan: ya tienen decision tomada, no reclaman
   atencion del propietario.
2. `SignalOutcome`: `signals.outcome_at_14d` (columna ya presente,
   `0006_signals.py`) es donde un futuro `MaintenanceCycle` de resolucion
   escribiria el contraste real; ningun proceso la rellena todavia. Hasta
   entonces se derivan solo los estados temporales bien definidos
   (`not_applicable` para HOLD, `in_progress` dentro de los 14 dias) y
   `pending` como resultado honesto de "sin contraste calculado" una vez
   cerrada la ventana -- nunca se inventa `confirmed`/`not_confirmed`."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.panel.application.dto import (
    ActionTaken,
    AnomalyView,
    Badges,
    BrakeBadge,
    ConnectionLevel,
    ConnectionsBadge,
    CreativeBadges,
    DegradedAccount,
    EntityChild,
    EntityDetail,
    EntityHistoryEntry,
    EntityMetrics,
    MetricPoint,
    PacingView,
    PortfolioRow,
    PortfolioView,
    ProposalBadges,
    SignalBadges,
    SignalDetailView,
    SignalRef,
    SignalsPage,
    SignalView,
)
from safent_ads.panel.application.dto import (
    BrakeMode as PanelBrakeMode,
)
from safent_ads.panel.application.dto import (
    LearningState as PanelLearningState,
)
from safent_ads.panel.application.entity_capabilities import entity_capabilities
from safent_ads.panel.application.errors import PanelEntityNotFoundError
from safent_ads.rules.application.read_models.caps_and_pacing import (
    caps_and_pacing,
)
from safent_ads.rules.application.read_models.caps_and_pacing import (
    days_in_month as _days_in_month,
)
from safent_ads.rules.application.read_models.caps_and_pacing import (
    money as _money,
)
from safent_ads.rules.application.read_models.caps_and_pacing import (
    to_major as _to_major,
)
from safent_ads.rules.domain.emergency_brake import BrakeMode, BrakeScope, BrakeScopeKind
from safent_ads.rules.infrastructure.read_models.caps_and_pacing import SqlAccountRefReadPort
from safent_ads.rules.infrastructure.sql_repositories import (
    SqlEmergencyBrakeRepository,
    SqlGuardrailRepository,
)
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.ids import BusinessId
from safent_ads.shared.read_models.dto import (
    Freshness,
    Money,
    SignalOutcome,
    SignalOutcomeStatus,
    SpendBreakdown,
)
from safent_ads.signals.domain.errors import ZeroBaselineError
from safent_ads.signals.domain.pacing import pace_index, projected_spend

# "TODAY" (026, T007): la unica ventana que el cockpit necesita y que
# `/portfolio` no tenia -- un dia, el de hoy, mismo criterio que
# `spend_today_minor` ya calcula por separado mas abajo.
_WINDOW_DAYS: Mapping[str, int] = {"TODAY": 1, "7D": 7, "14D": 14, "30D": 30}
_SPEND_14D_DAYS = 14
_OUTCOME_WINDOW_DAYS = 14
_NOT_APPLICABLE_SIGNAL_KINDS = frozenset({"HOLD"})
_MIN_CONFIRMED_RATE_SAMPLE = 10  # contracts/rest-api.md: "no muestra tasa con muestra <10"
_FRESHNESS_STALE_AFTER_MINUTES = 60  # NFR-1: frescura <= 60 min en horario activo

_SELECT_ENTITIES_FOR_BUSINESS = text("""
    SELECT e.entity_ref, e.name, e.status, e.is_controllable, e.learning_state,
           e.budget_amount_minor, e.budget_currency,
           pa.id AS platform_account_id, pa.account_ref AS platform_account_ref,
           pa.platform, pa.currency, pa.status AS account_status
      FROM ad_entities_physical e
      JOIN platform_accounts pa ON pa.id = e.platform_account_id
     WHERE e.business_id = :business_id AND e.level = 'campaign'
     ORDER BY e.name
""")

_SELECT_SPEND_IN_WINDOW = text("""
    SELECT physical_entity_ref AS entity_ref, COALESCE(SUM(spend), 0) AS spend_minor,
           COALESCE(SUM(conversions_lead), 0) AS conversions_lead
      FROM metrics_daily_physical
     WHERE business_id = :business_id AND entity_level = 'campaign'
       AND stat_date BETWEEN :start AND :end
     GROUP BY physical_entity_ref
""")

_SELECT_DAILY_SERIES = text("""
    SELECT physical_entity_ref AS entity_ref, stat_date, COALESCE(SUM(spend), 0) AS spend_minor
      FROM metrics_daily_physical
     WHERE business_id = :business_id AND entity_level = 'campaign'
       AND stat_date BETWEEN :start AND :end
     GROUP BY physical_entity_ref, stat_date
""")

_SELECT_BUSINESS_SPEND_TODAY = text("""
    SELECT COALESCE(SUM(spend), 0) AS spend_minor
      FROM metrics_daily_physical
     WHERE business_id = :business_id AND entity_level = 'campaign' AND stat_date = :today
""")

_SELECT_BUSINESS_SPEND_MTD = text("""
    SELECT COALESCE(SUM(spend), 0) AS spend_minor
      FROM metrics_daily_physical
     WHERE business_id = :business_id AND entity_level = 'campaign'
       AND stat_date BETWEEN :month_start AND :today
""")

_SELECT_LATEST_SIGNAL_PER_ENTITY = text("""
    SELECT DISTINCT ON (entity_ref) entity_ref, kind, strength, cause,
           money_at_stake_minor, money_at_stake_currency
      FROM signals
     WHERE business_id = :business_id
     ORDER BY entity_ref, emitted_at DESC
""")

_SELECT_FRESHNESS_PER_ACCOUNT = text("""
    SELECT pa.id AS platform_account_id, pa.platform, pa.external_account_id,
           MAX(m.ingested_at) AS last_ingested_at
      FROM platform_accounts pa
      LEFT JOIN platform_accounts observed ON observed.business_id=pa.business_id
        AND observed.platform=pa.platform AND observed.external_account_id=pa.external_account_id
      LEFT JOIN metrics_daily m ON m.platform_account_id = observed.id
     WHERE pa.business_id = :business_id
     GROUP BY pa.id, pa.platform, pa.external_account_id
""")

_SELECT_ENTITY = text("""
    SELECT e.business_id, e.entity_ref, e.name, e.status, e.platform_state_hash,
           e.is_controllable, e.learning_state, e.level
      FROM ad_entities e
     WHERE e.entity_ref = :entity_ref
""")

_SELECT_CHILDREN = text("""
    SELECT e.entity_ref, e.name, e.level, e.status, e.is_controllable,
           e.learning_state, e.budget_kind, a.currency, m.*,
           s.kind AS signal_kind, s.strength AS signal_strength, s.cause AS signal_cause,
           EXISTS (SELECT 1 FROM ad_entities child WHERE child.parent_id=e.id
                     AND child.business_id=e.business_id
                     AND child.platform_account_id=e.platform_account_id) AS has_children
      FROM ad_entities parent
      JOIN ad_entities e ON e.parent_id=parent.id AND e.business_id=parent.business_id
                        AND e.platform_account_id=parent.platform_account_id
      JOIN platform_accounts a ON a.id=e.platform_account_id AND a.business_id=e.business_id
      LEFT JOIN LATERAL (
          SELECT count(*) AS fact_count,
                 count(*) FILTER (WHERE stat_date=:today) AS today_fact_count,
                 SUM(spend) FILTER (WHERE stat_date=:today) AS spend_today_minor,
                 SUM(spend) AS spend_window_minor,
                 SUM(conversions_lead) AS leads, SUM(conversions_whatsapp) AS whatsapp,
                 SUM(conversions_call) AS calls,
                 SUM(conversions_business_conversion) AS business_conversions,
                 MAX(ingested_at) AS last_ingested_at
            FROM metrics_daily
           WHERE entity_ref=e.entity_ref AND business_id=e.business_id
             AND platform_account_id=e.platform_account_id
             AND stat_date BETWEEN :start AND :today
      ) m ON true
      LEFT JOIN LATERAL (
          SELECT kind, strength, cause FROM signals
           WHERE entity_ref=e.entity_ref AND business_id=e.business_id
           ORDER BY emitted_at DESC, id DESC LIMIT 1
      ) s ON true
     WHERE parent.entity_ref=:entity_ref
     ORDER BY e.name, e.entity_ref
""")

_SELECT_ENTITY_METRICS_DAILY = text("""
    SELECT stat_date AS period_start, currency, SUM(spend) AS spend_minor,
           SUM(conversions_lead + conversions_whatsapp + conversions_call
               + conversions_business_conversion) AS conversions
      FROM metrics_daily
     WHERE entity_ref = :entity_ref AND stat_date BETWEEN :start AND :end
     GROUP BY stat_date, currency
     ORDER BY stat_date
""")

_SELECT_SIGNALS_PAGE = text("""
    SELECT id, business_id, entity_ref, kind, strength, cause,
           money_at_stake_minor, money_at_stake_currency, data_window,
           emitted_at, outcome_at_14d
      FROM signals
     WHERE business_id = :business_id
       AND (CAST(:kind AS TEXT) IS NULL OR kind = CAST(:kind AS TEXT))
       AND (
           CAST(:min_strength AS SMALLINT) IS NULL
           OR strength >= CAST(:min_strength AS SMALLINT)
       )
       AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR emitted_at >= CAST(:since AS TIMESTAMPTZ))
       AND (CAST(:entity_ref AS TEXT) IS NULL OR entity_ref = CAST(:entity_ref AS TEXT))
     ORDER BY emitted_at DESC
     LIMIT :limit
""")

_SELECT_SIGNAL_BY_ID = text("""
    SELECT id, business_id, entity_ref, kind, strength, cause,
           money_at_stake_minor, money_at_stake_currency, data_window,
           emitted_at, outcome_at_14d, gate_verdicts, evidence
      FROM signals
     WHERE id = :signal_id
""")

_SELECT_ANOMALIES = text("""
    SELECT id, business_id, entity_ref, method, score, severity
      FROM anomalies
     WHERE business_id = :business_id AND detected_at >= :since
     ORDER BY detected_at DESC
""")

_SELECT_PACING_INPUTS = text("""
    SELECT e.business_id, e.budget_amount_minor, e.budget_currency,
           COALESCE((SELECT SUM(spend) FROM metrics_daily m
                      WHERE m.entity_ref = e.entity_ref
                        AND m.stat_date BETWEEN :month_start AND :today), 0) AS mtd_spend_minor
      FROM ad_entities e
     WHERE e.entity_ref = :entity_ref
""")

_SELECT_ACCOUNT_REF_FOR_ENTITY = text("""
    SELECT pa.platform, pa.external_account_id, pa.account_ref
      FROM ad_entities e
      JOIN platform_accounts pa ON pa.id = e.platform_account_id
     WHERE e.entity_ref = :entity_ref
""")

_COUNT_NEW_SIGNALS_SINCE = text("""
    SELECT COUNT(*) AS n FROM signals WHERE business_id = :business_id AND emitted_at >= :since
""")

# `pending`/`critical`/`deferred`: ver docstring del modulo, punto 1.
_COUNT_PROPOSAL_BADGES = text("""
    SELECT
        count(*) FILTER (WHERE state = 'pending')                             AS pending,
        count(*) FILTER (WHERE state = 'pending' AND urgency = 'critical')    AS critical,
        count(*) FILTER (WHERE state = 'postponed')                          AS deferred
    FROM proposals
    WHERE business_id = :business_id
""")


@dataclass(frozen=True, slots=True)
class _ProposalCounts:
    pending: int
    critical: int
    deferred: int


def _window_start(window: str, today: date) -> date:
    days = _WINDOW_DAYS.get(window, 7)
    return today - timedelta(days=days - 1)


def _month_start(today: date) -> date:
    return today.replace(day=1)


class SqlPanelReadPort:
    """Una instancia por peticion HTTP (`container.session_factory()` en
    `panel/presentation/rest.py`, mismo patron que `iam`/`audit`).
    `clock` inyectable (plan.md N0): el dominio/infraestructura nunca llama
    `datetime.now` directamente para que "hoy"/"frescura" sean
    deterministas en pruebas."""

    def __init__(self, session: AsyncSession, clock: Clock | None = None) -> None:
        self._session = session
        self._clock = clock or SystemClock()

    # -- Cartera -----------------------------------------------------

    async def get_portfolio(self, business_id: str, *, window: str) -> PortfolioView:
        business_uuid = uuid.UUID(business_id)
        today = self._clock.now().date()
        window_start = _window_start(window, today)
        month_start = _month_start(today)

        entities = (
            (
                await self._session.execute(
                    _SELECT_ENTITIES_FOR_BUSINESS, {"business_id": business_uuid}
                )
            )
            .mappings()
            .all()
        )
        spend_rows = {
            row["entity_ref"]: row
            for row in (
                await self._session.execute(
                    _SELECT_SPEND_IN_WINDOW,
                    {"business_id": business_uuid, "start": window_start, "end": today},
                )
            ).mappings()
        }
        series_rows = (
            await self._session.execute(
                _SELECT_DAILY_SERIES,
                {
                    "business_id": business_uuid,
                    "start": today - timedelta(days=_SPEND_14D_DAYS - 1),
                    "end": today,
                },
            )
        ).mappings()
        series_by_entity: dict[str, dict[date, int]] = {}
        for row in series_rows:
            series_by_entity.setdefault(row["entity_ref"], {})[row["stat_date"]] = int(
                row["spend_minor"]
            )
        signal_rows = {
            row["entity_ref"]: row
            for row in (
                await self._session.execute(
                    _SELECT_LATEST_SIGNAL_PER_ENTITY, {"business_id": business_uuid}
                )
            ).mappings()
        }

        rows = [
            self._portfolio_row(entity, spend_rows, series_by_entity, signal_rows, today)
            for entity in entities
        ]

        spend_today_minor = int(
            (
                await self._session.execute(
                    _SELECT_BUSINESS_SPEND_TODAY, {"business_id": business_uuid, "today": today}
                )
            ).scalar_one()
        )
        spend_mtd_minor = int(
            (
                await self._session.execute(
                    _SELECT_BUSINESS_SPEND_MTD,
                    {"business_id": business_uuid, "month_start": month_start, "today": today},
                )
            ).scalar_one()
        )
        currency = (
            entities[0]["currency"]
            if entities
            else (
                await self._session.execute(
                    text("SELECT reference_currency FROM businesses WHERE id = :business_id"),
                    {"business_id": business_uuid},
                )
            ).scalar_one()
        )
        window_spend_minor = sum(int(row.get("spend_minor", 0)) for row in spend_rows.values())

        caps, pacing, is_partial = await caps_and_pacing(
            SqlAccountRefReadPort(self._session),
            SqlGuardrailRepository(self._session),
            business_id=business_uuid,
            spend_mtd_minor=spend_mtd_minor,
            today=today,
            month_start=month_start,
        )
        freshness_rows = (
            (
                await self._session.execute(
                    _SELECT_FRESHNESS_PER_ACCOUNT, {"business_id": business_uuid}
                )
            )
            .mappings()
            .all()
        )
        freshness = _business_freshness(freshness_rows, now=self._clock.now())
        degraded_by_account: dict[str, DegradedAccount] = {
            # Keyed by the same canonical `account_ref` `PortfolioRow.platform_account_id`
            # now carries (hotfix 0.2.20, Bug C) so the panel can match a row to its
            # degraded account without going through the internal UUID.
            row["platform_account_ref"]: DegradedAccount(
                platform_account_id=row["platform_account_ref"],
                platform=row["platform"],
                status=row["account_status"],
                reason="platform_account_status_not_active",
            )
            for row in entities
            if row["account_status"] != "ACTIVE"
        }
        degraded = list(degraded_by_account.values())

        conversions_by_kind = {
            "lead": sum(int(row.get("conversions_lead", 0)) for row in spend_rows.values())
        }

        proposal_counts = await self._proposal_badge_counts(business_uuid)

        return PortfolioView(
            window=window,
            currency=currency,
            spend=SpendBreakdown(
                window=_money(window_spend_minor, currency),
                today=_money(spend_today_minor, currency),
                mtd=_money(spend_mtd_minor, currency),
            ),
            caps=caps,
            pacing=pacing,
            conversions_by_kind=conversions_by_kind,
            cost_per_lead=_cost_per_lead(window_spend_minor, conversions_by_kind["lead"], currency),
            cost_per_business_conversion=None,
            pending_proposals=proposal_counts.pending,
            deferred_proposals=proposal_counts.deferred,
            freshness=freshness,
            deviation_vs_platform_pct=None,
            is_partial=is_partial,
            degraded_accounts=degraded,
            rows=rows,
        )

    def _portfolio_row(
        self,
        entity: RowMapping,
        spend_rows: Mapping[str, RowMapping],
        series_by_entity: Mapping[str, Mapping[date, int]],
        signal_rows: Mapping[str, RowMapping],
        today: date,
    ) -> PortfolioRow:
        entity_ref = entity["entity_ref"]
        currency = entity["currency"]
        spend_row = spend_rows.get(entity_ref)
        spend_minor = int(spend_row["spend_minor"]) if spend_row else 0
        conversions_lead = int(spend_row["conversions_lead"]) if spend_row else 0
        signal_row = signal_rows.get(entity_ref)
        series = series_by_entity.get(entity_ref, {})
        spend_14d = [
            float(_to_major(series.get(today - timedelta(days=offset), 0)))
            for offset in range(_SPEND_14D_DAYS - 1, -1, -1)
        ]
        return PortfolioRow(
            entity_ref=entity_ref,
            name=entity["name"],
            platform=entity["platform"],
            platform_account_id=entity["platform_account_ref"],
            platform_account_uuid=str(entity["platform_account_id"]),
            status=entity["status"],
            currency=currency,
            budget=_money(entity["budget_amount_minor"], entity["budget_currency"] or currency),
            spend=_money(spend_minor, currency),
            cost_per_lead=_cost_per_lead(spend_minor, conversions_lead, currency),
            signal=(
                SignalRef(
                    kind=signal_row["kind"],
                    strength=int(signal_row["strength"]),
                    cause=signal_row["cause"],
                )
                if signal_row
                else None
            ),
            money_at_stake=(
                _money(
                    int(signal_row["money_at_stake_minor"]),
                    signal_row["money_at_stake_currency"],
                )
                if signal_row
                else Money(Decimal(0), currency)
            ),
            is_controllable=entity["is_controllable"],
            learning_state=PanelLearningState(
                is_learning=entity["learning_state"] == "LEARNING", reason=None, since=None
            ),
            spend_14d=spend_14d,
            is_degraded=entity["account_status"] != "ACTIVE",
        )

    async def _proposal_badge_counts(self, business_id: uuid.UUID) -> _ProposalCounts:
        row = (
            (await self._session.execute(_COUNT_PROPOSAL_BADGES, {"business_id": business_id}))
            .mappings()
            .one()
        )
        return _ProposalCounts(
            pending=int(row["pending"]),
            critical=int(row["critical"]),
            deferred=int(row["deferred"]),
        )

    # -- Entidades -----------------------------------------------------

    async def get_entity(self, entity_ref: str) -> EntityDetail:
        row = (
            (await self._session.execute(_SELECT_ENTITY, {"entity_ref": entity_ref}))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PanelEntityNotFoundError(entity_ref)
        capabilities = entity_capabilities(
            status=row["status"], is_controllable=row["is_controllable"]
        )
        return EntityDetail(
            business_id=str(row["business_id"]),
            entity_ref=row["entity_ref"],
            name=row["name"],
            status=row["status"],
            platform_state_hash=row["platform_state_hash"],
            is_controllable=row["is_controllable"],
            learning_state=row["learning_state"].lower(),
            can_pause=capabilities.can_pause,
            can_resume=capabilities.can_resume,
            can_delete=capabilities.can_delete,
            level=row["level"],
        )

    async def get_entity_children(self, entity_ref: str) -> list[EntityChild]:
        await self.get_entity(entity_ref)  # 404 si no existe
        now = self._clock.now()
        today = now.date()
        rows = (
            await self._session.execute(
                _SELECT_CHILDREN,
                {"entity_ref": entity_ref, "today": today, "start": _window_start("7D", today)},
            )
        ).mappings()
        return [_entity_child(row, now) for row in rows]

    async def get_entity_metrics(
        self, entity_ref: str, *, window: str, granularity: str
    ) -> EntityMetrics:
        del granularity  # solo diario por ahora: metrics_hourly tiene retencion de 14d
        await self.get_entity(entity_ref)
        today = self._clock.now().date()
        start = _window_start(window, today)
        rows = (
            await self._session.execute(
                _SELECT_ENTITY_METRICS_DAILY,
                {"entity_ref": entity_ref, "start": start, "end": today},
            )
        ).mappings()
        points = [
            MetricPoint(
                period_start=datetime.combine(row["period_start"], datetime.min.time(), UTC),
                spend=_money(int(row["spend_minor"]), row["currency"]),
                conversions=int(row["conversions"]),
            )
            for row in rows
        ]
        return EntityMetrics(entity_ref=entity_ref, granularity="daily", points=points)

    async def get_entity_history(self, entity_ref: str) -> list[EntityHistoryEntry]:
        # `decision_log` (audit) es la fuente real de historial; sin filtro
        # por `entity_ref` expuesto todavia en `audit.application.ports`
        # (busca por `business_id` + rango). Vacio en vez de fallar: el
        # contrato pide una lista, no un error, cuando no hay historial.
        await self.get_entity(entity_ref)
        return []

    # -- Frescura --------------------------------------------------------

    async def get_freshness(self, business_id: str) -> list[Freshness]:
        rows = (
            (
                await self._session.execute(
                    _SELECT_FRESHNESS_PER_ACCOUNT, {"business_id": uuid.UUID(business_id)}
                )
            )
            .mappings()
            .all()
        )
        now = self._clock.now()
        return [_account_freshness(row, now) for row in rows]

    # -- Senales -----------------------------------------------------

    async def list_signals(
        self,
        business_id: str,
        *,
        kind: str | None,
        min_strength: int | None,
        since: datetime | None,
        platform: str | None,
        entity_ref: str | None,
        limit: int | None,
        cursor: str | None,
    ) -> SignalsPage:
        del platform, cursor  # sin paginacion por cursor todavia (LIMIT simple)
        rows = (
            (
                await self._session.execute(
                    _SELECT_SIGNALS_PAGE,
                    {
                        "business_id": uuid.UUID(business_id),
                        "kind": kind.upper() if kind else None,
                        "min_strength": min_strength,
                        "since": since,
                        "entity_ref": entity_ref,
                        "limit": limit or 50,
                    },
                )
            )
            .mappings()
            .all()
        )
        now = self._clock.now()
        items = [self._signal_view(row, now) for row in rows]
        resolved = [item for item in items if item.outcome.status != SignalOutcomeStatus.PENDING]
        confirmed = sum(
            1 for item in resolved if item.outcome.status == SignalOutcomeStatus.CONFIRMED
        )
        sample = len(resolved)
        return SignalsPage(
            items=items,
            next_cursor=None,
            confirmed_rate_pct=(
                confirmed / sample * 100 if sample >= _MIN_CONFIRMED_RATE_SAMPLE else None
            ),
            confirmed_rate_sample=sample,
        )

    async def get_signal(self, signal_id: str) -> SignalDetailView:
        row = (
            (await self._session.execute(_SELECT_SIGNAL_BY_ID, {"signal_id": uuid.UUID(signal_id)}))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PanelEntityNotFoundError(signal_id)
        signal = self._signal_view(row, self._clock.now())
        return SignalDetailView(
            signal=signal,
            gate_verdicts=[str(v) for v in (row["gate_verdicts"] or [])],
            evidence=[str(row["evidence"])] if row["evidence"] else [],
            narrative=signal.cause,
        )

    def _signal_view(self, row: RowMapping, now: datetime) -> SignalView:
        emitted_at = row["emitted_at"]
        return SignalView(
            signal_id=str(row["id"]),
            business_id=str(row["business_id"]),
            entity_ref=row["entity_ref"],
            entity_name=row["entity_ref"],
            platform=row["entity_ref"].split(":", 1)[0],
            kind=row["kind"],
            strength=int(row["strength"]),
            cause=row["cause"],
            money_at_stake=_money(
                int(row["money_at_stake_minor"]),
                row["money_at_stake_currency"],
            ),
            data_window=row["data_window"],
            action_taken=ActionTaken.NO_ACTION,
            proposal_id=None,
            emitted_at=emitted_at,
            outcome=_signal_outcome(row["kind"], emitted_at, row.get("outcome_at_14d"), now),
        )

    # -- Anomalias / pacing -----------------------------------------------

    async def list_anomalies(self, business_id: str, *, since: datetime) -> list[AnomalyView]:
        rows = (
            await self._session.execute(
                _SELECT_ANOMALIES, {"business_id": uuid.UUID(business_id), "since": since}
            )
        ).mappings()
        return [
            AnomalyView(
                anomaly_id=str(row["id"]),
                business_id=str(row["business_id"]),
                entity_ref=row["entity_ref"],
                method=row["method"],
                score=float(row["score"]),
                severity=row["severity"].lower(),
            )
            for row in rows
        ]

    async def get_pacing(self, entity_ref: str) -> PacingView:
        detail = await self.get_entity(entity_ref)
        today = self._clock.now().date()
        month_start = _month_start(today)
        row = (
            (
                await self._session.execute(
                    _SELECT_PACING_INPUTS,
                    {"entity_ref": entity_ref, "month_start": month_start, "today": today},
                )
            )
            .mappings()
            .one_or_none()
        )
        account_ref_row = (
            (
                await self._session.execute(
                    _SELECT_ACCOUNT_REF_FOR_ENTITY, {"entity_ref": entity_ref}
                )
            )
            .mappings()
            .one()
        )
        account_ref = account_ref_row["account_ref"]
        policy = await SqlGuardrailRepository(self._session).find_for_account(
            account_ref=account_ref
        )
        currency = row["budget_currency"] or "EUR" if row else "EUR"
        if row is None or policy is None:
            zero = Money(Decimal(0), currency)
            return PacingView(entity_ref, detail.business_id, 0.0, zero, zero, zero)

        mtd_minor = int(row["mtd_spend_minor"])
        days_in_month = _days_in_month(today)
        days_elapsed = (today - month_start).days + 1
        try:
            index = pace_index(
                actual_mtd_minor=mtd_minor,
                monthly_cap_minor=policy.monthly_cap_minor,
                days_elapsed=days_elapsed,
                days_in_month=days_in_month,
            )
            projection_minor = int(
                projected_spend(
                    actual_mtd_minor=mtd_minor,
                    days_elapsed=days_elapsed,
                    days_in_month=days_in_month,
                )
            )
        except ZeroBaselineError:
            index, projection_minor = 0.0, mtd_minor
        remaining_minor = max(policy.monthly_cap_minor - mtd_minor, 0)
        days_left = max(days_in_month - days_elapsed, 1)
        return PacingView(
            entity_ref=entity_ref,
            business_id=detail.business_id,
            pace_index=round(index, 3),
            projected=_money(projection_minor, currency),
            remaining=_money(remaining_minor, currency),
            new_daily=_money(round(remaining_minor / days_left), currency),
        )

    # -- Insignias ---------------------------------------------------

    async def get_badges(self, business_id: str, *, signals_since: datetime | None) -> Badges:
        business_uuid = uuid.UUID(business_id)
        since = signals_since or (self._clock.now() - timedelta(days=1))
        new_signals = int(
            (
                await self._session.execute(
                    _COUNT_NEW_SIGNALS_SINCE, {"business_id": business_uuid, "since": since}
                )
            ).scalar_one()
        )
        brakes = SqlEmergencyBrakeRepository(self._session)
        global_brake = await brakes.find_active(scope=BrakeScope(kind=BrakeScopeKind.GLOBAL))
        business_brake = await brakes.find_active(
            scope=BrakeScope(kind=BrakeScopeKind.BUSINESS, business_id=BusinessId(business_uuid))
        )
        active_brake = global_brake or business_brake
        proposal_counts = await self._proposal_badge_counts(business_uuid)
        return Badges(
            proposals=ProposalBadges(
                pending=proposal_counts.pending,
                critical=proposal_counts.critical,
                deferred=proposal_counts.deferred,
            ),
            # `creative` sigue en memoria: sin fila real que contar (ver docstring del modulo).
            creatives=CreativeBadges(pending_approval=0),
            signals=SignalBadges(new_since=new_signals, since=since),
            connections=ConnectionsBadge(level=ConnectionLevel.OK, reason=None),
            brake=BrakeBadge(
                engaged=active_brake is not None,
                mode=(
                    PanelBrakeMode.ALL
                    if active_brake and active_brake.mode == BrakeMode.ALL
                    else PanelBrakeMode.AUTONOMOUS
                    if active_brake
                    else None
                ),
            ),
        )


def _entity_child(row: RowMapping, now: datetime) -> EntityChild:
    has_facts = row["fact_count"] > 0
    currency = row["currency"]
    freshness = _account_freshness(row, now) if has_facts else None
    badges = []
    if not row["is_controllable"]:
        badges.append("no_controlable")
    if row["learning_state"] == "LEARNING":
        badges.append("en_aprendizaje")
    if row["budget_kind"] == "shared":
        badges.append("presupuesto_compartido")
    if freshness is not None and freshness.is_stale:
        badges.append("obsoleto")
    return EntityChild(
        entity_ref=row["entity_ref"],
        name=row["name"],
        level=row["level"],
        status=row["status"],
        spend_today=(
            _money(int(row["spend_today_minor"]), currency) if row["today_fact_count"] else None
        ),
        spend_window=_money(int(row["spend_window_minor"]), currency) if has_facts else None,
        conversions_by_kind={
            "lead": int(row["leads"]),
            "whatsapp": int(row["whatsapp"]),
            "call": int(row["calls"]),
            "business_conversion": int(row["business_conversions"]),
        }
        if has_facts
        else None,
        cost_per_lead=(
            _cost_per_lead(int(row["spend_window_minor"]), int(row["leads"]), currency)
            if has_facts
            else None
        ),
        cost_per_business_conversion=(
            _cost_per_lead(
                int(row["spend_window_minor"]), int(row["business_conversions"]), currency
            )
            if has_facts
            else None
        ),
        signal=SignalRef(row["signal_kind"], int(row["signal_strength"]), row["signal_cause"])
        if row["signal_kind"] is not None
        else None,
        freshness=freshness,
        badges=badges,
        has_children=row["has_children"],
    )


def _cost_per_lead(spend_minor: int, conversions_lead: int, currency: str) -> Money | None:
    if conversions_lead <= 0:
        return None
    return Money(_to_major(round(spend_minor / conversions_lead)), currency)


def _account_freshness(row: RowMapping, now: datetime) -> Freshness:
    last_ingested_at = row["last_ingested_at"]
    if last_ingested_at is None:
        # Nunca ingerido: NFR-1 pide frescura "siempre visible", nunca un
        # campo nulo -- pero "sin estadisticas todavia" NO es "datos viejos"
        # (hotfix 0.2.20, Bug B): no_data=True, is_stale=False, sin inventar
        # una edad ficticia que la escritura ("> 60 min") pueda bloquear.
        return Freshness(last_ingested_at=now, lag_minutes=0, is_stale=False, no_data=True)
    lag_minutes = int((now - last_ingested_at).total_seconds() // 60)
    return Freshness(
        last_ingested_at=last_ingested_at,
        lag_minutes=lag_minutes,
        is_stale=lag_minutes > _FRESHNESS_STALE_AFTER_MINUTES,
    )


def _business_freshness(rows: Sequence[RowMapping], now: datetime) -> Freshness:
    if not rows:
        return Freshness(last_ingested_at=now, lag_minutes=0, is_stale=False, no_data=True)
    per_account = [_account_freshness(row, now) for row in rows]
    # A sibling account that never ingested anything must never mask a real,
    # possibly stale, account: pick the worst among accounts that HAVE data;
    # only report `no_data` for the whole business when none of them do
    # (hotfix 0.2.20, Bug B).
    with_data = [item for item in per_account if not item.no_data]
    if not with_data:
        return per_account[0]
    return max(with_data, key=lambda f: f.lag_minutes)


def _signal_outcome(
    kind: str, emitted_at: datetime, outcome_at_14d: object, now: datetime
) -> SignalOutcome:
    if kind in _NOT_APPLICABLE_SIGNAL_KINDS:
        return SignalOutcome(
            status=SignalOutcomeStatus.NOT_APPLICABLE, days_remaining=None, evaluated_at=None
        )
    if outcome_at_14d:
        # Forma futura de un resolutor de `MaintenanceCycle` que todavia no
        # existe (ver docstring del modulo): se respeta si algun dia llega
        # poblada, nunca se inventa aqui.
        status_raw = outcome_at_14d.get("status") if isinstance(outcome_at_14d, dict) else None
        if status_raw in (SignalOutcomeStatus.CONFIRMED, SignalOutcomeStatus.NOT_CONFIRMED):
            return SignalOutcome(
                status=SignalOutcomeStatus(status_raw), days_remaining=None, evaluated_at=now
            )
    days_since = (now - emitted_at).days
    if days_since < _OUTCOME_WINDOW_DAYS:
        return SignalOutcome(
            status=SignalOutcomeStatus.IN_PROGRESS,
            days_remaining=_OUTCOME_WINDOW_DAYS - days_since,
            evaluated_at=None,
        )
    # Ventana cerrada sin resolutor: honesto "sin contraste calculado
    # todavia", nunca un `confirmed`/`not_confirmed` inventado.
    return SignalOutcome(status=SignalOutcomeStatus.PENDING, days_remaining=None, evaluated_at=None)


class RequestScopedPanelReadPort:
    """`PanelReadPort` real, una sesion por llamada
    (`container.session_factory()`, mismo patron que `iam`/`audit` en sus
    routers): `build_panel_router` construye un unico `PanelReadPort` al
    arrancar la app (`composition/app.py`), asi que esta envoltura es la
    que hace que cada peticion HTTP tenga su propia sesion en vez de
    compartir una entre todas."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], clock: Clock | None = None
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    async def get_portfolio(self, business_id: str, *, window: str) -> PortfolioView:
        async with self._session_factory() as session:
            return await SqlPanelReadPort(session, self._clock).get_portfolio(
                business_id, window=window
            )

    async def get_entity(self, entity_ref: str) -> EntityDetail:
        async with self._session_factory() as session:
            return await SqlPanelReadPort(session, self._clock).get_entity(entity_ref)

    async def get_entity_children(self, entity_ref: str) -> list[EntityChild]:
        async with self._session_factory() as session:
            return await SqlPanelReadPort(session, self._clock).get_entity_children(entity_ref)

    async def get_entity_metrics(
        self, entity_ref: str, *, window: str, granularity: str
    ) -> EntityMetrics:
        async with self._session_factory() as session:
            return await SqlPanelReadPort(session, self._clock).get_entity_metrics(
                entity_ref, window=window, granularity=granularity
            )

    async def get_entity_history(self, entity_ref: str) -> list[EntityHistoryEntry]:
        async with self._session_factory() as session:
            return await SqlPanelReadPort(session, self._clock).get_entity_history(entity_ref)

    async def get_freshness(self, business_id: str) -> list[Freshness]:
        async with self._session_factory() as session:
            return await SqlPanelReadPort(session, self._clock).get_freshness(business_id)

    async def list_signals(
        self,
        business_id: str,
        *,
        kind: str | None,
        min_strength: int | None,
        since: datetime | None,
        platform: str | None,
        entity_ref: str | None,
        limit: int | None,
        cursor: str | None,
    ) -> SignalsPage:
        async with self._session_factory() as session:
            return await SqlPanelReadPort(session, self._clock).list_signals(
                business_id,
                kind=kind,
                min_strength=min_strength,
                since=since,
                platform=platform,
                entity_ref=entity_ref,
                limit=limit,
                cursor=cursor,
            )

    async def get_signal(self, signal_id: str) -> SignalDetailView:
        async with self._session_factory() as session:
            return await SqlPanelReadPort(session, self._clock).get_signal(signal_id)

    async def list_anomalies(self, business_id: str, *, since: datetime) -> list[AnomalyView]:
        async with self._session_factory() as session:
            return await SqlPanelReadPort(session, self._clock).list_anomalies(
                business_id, since=since
            )

    async def get_pacing(self, entity_ref: str) -> PacingView:
        async with self._session_factory() as session:
            return await SqlPanelReadPort(session, self._clock).get_pacing(entity_ref)

    async def get_badges(self, business_id: str, *, signals_since: datetime | None) -> Badges:
        async with self._session_factory() as session:
            return await SqlPanelReadPort(session, self._clock).get_badges(
                business_id, signals_since=signals_since
            )
