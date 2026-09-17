"""`SignalReadPort` real (integracion, wiring2) sobre `signals`/`anomalies`
(`0006_signals.py`, `0012_domain_alignment.py`) -- mismas tablas que
`panel.infrastructure.sql_read_model`, proyectadas al DTO propio de `mcp`
(plan.md §4: sin importar `panel`).

`_derive_outcome` reimplementa a proposito la misma derivacion de 5 estados
que `panel.infrastructure.sql_read_model._signal_outcome` (NOT_APPLICABLE
para HOLD, IN_PROGRESS dentro de los 14 dias, PENDING si la ventana cerro
sin resolutor, CONFIRMED/NOT_CONFIRMED solo si `signals.outcome_at_14d` ya
viene poblada) -- son ~10 lineas de logica pura sobre la misma columna,
duplicarlas es mas barato que acoplar `mcp` a `panel` (mismo principio que
`accounts.infrastructure.broker_client` documenta para el framing del
socket del broker).

`list_signals` no pagina por cursor todavia (`LIMIT` simple): mismo hueco
que `panel` ya documenta ("sin paginacion por cursor todavia") -- se
respeta aqui en vez de inventar un cursor que ningun cliente puede usar
para pedir la pagina siguiente de verdad."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from safent_ads.mcp.application.dto import (
    AnomalySummary,
    Cause,
    Evidence,
    GateVerdict,
    PacingInfo,
    SignalDetail,
    SignalKind,
    SignalsPage,
    SignalSummary,
)
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.rules.infrastructure.sql_repositories import SqlGuardrailRepository
from safent_ads.shared.clock import Clock, SystemClock
from safent_ads.shared.read_models.dto import (
    Money,
    SignalOutcome,
    SignalOutcomeStatus,
)
from safent_ads.signals.domain.errors import ZeroBaselineError
from safent_ads.signals.domain.pacing import pace_index, projected_spend

_MINOR_UNITS_PER_MAJOR = 100
_DEFAULT_LIMIT = 50
_OUTCOME_WINDOW_DAYS = 14
_MIN_CONFIRMED_RATE_SAMPLE = 10
_NOT_APPLICABLE_KINDS = frozenset({"HOLD"})

_SELECT_SIGNALS = text("""
    SELECT id, entity_ref, kind, strength, cause, rule_code, money_at_stake_minor,
           money_at_stake_currency, data_window, emitted_at, outcome_at_14d
      FROM signals
     WHERE business_id = :business_id
       AND (CAST(:kind AS TEXT) IS NULL OR kind = CAST(:kind AS TEXT))
       AND (
           CAST(:min_strength AS SMALLINT) IS NULL
           OR strength >= CAST(:min_strength AS SMALLINT)
       )
       AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR emitted_at >= CAST(:since AS TIMESTAMPTZ))
     ORDER BY emitted_at DESC
     LIMIT :limit
""")

_SELECT_SIGNAL = text("""
    SELECT id, entity_ref, kind, strength, cause, rule_code, money_at_stake_minor,
           money_at_stake_currency, data_window, emitted_at, outcome_at_14d,
           gate_verdicts, evidence
      FROM signals
     WHERE business_id = :business_id AND id = :signal_id
""")

_SELECT_ANOMALIES = text("""
    SELECT id, entity_ref, method, score, severity
      FROM anomalies
     WHERE business_id = :business_id AND detected_at >= :since
     ORDER BY detected_at DESC
""")

_SELECT_PACING_INPUTS = text("""
    SELECT e.budget_amount_minor, e.budget_currency, pa.platform,
           pa.external_account_id, pa.account_ref,
           COALESCE((SELECT SUM(spend) FROM metrics_daily m
                      WHERE m.entity_ref = e.entity_ref
                        AND m.stat_date BETWEEN :month_start AND :today), 0) AS mtd_spend_minor
      FROM ad_entities e
      JOIN platform_accounts pa ON pa.id = e.platform_account_id
     WHERE e.business_id = :business_id AND e.entity_ref = :entity_ref
""")


class SqlSignalReadPort:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], clock: Clock | None = None
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    async def list_signals(
        self,
        business_id: str,
        *,
        kind: str | None,
        min_strength: int | None,
        since: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> SignalsPage:
        del cursor  # ver docstring del modulo: sin paginacion por cursor todavia
        now = self._clock.now()
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        _SELECT_SIGNALS,
                        {
                            "business_id": business_id,
                            "kind": kind.upper() if kind else None,
                            "min_strength": min_strength,
                            "since": since,
                            "limit": limit or _DEFAULT_LIMIT,
                        },
                    )
                )
                .mappings()
                .all()
            )
        items = [_summary(row, now) for row in rows]
        resolved = [item for item in items if item.outcome.status != SignalOutcomeStatus.PENDING]
        confirmed = sum(
            1 for item in resolved if item.outcome.status == SignalOutcomeStatus.CONFIRMED
        )
        sample = len(resolved)
        return SignalsPage(
            items=items,
            cursor=None,
            confirmed_rate_pct=(
                confirmed / sample * 100 if sample >= _MIN_CONFIRMED_RATE_SAMPLE else None
            ),
            confirmed_rate_sample=sample,
        )

    async def get_signal(self, business_id: str, signal_id: str) -> SignalDetail:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        _SELECT_SIGNAL,
                        {"business_id": business_id, "signal_id": uuid.UUID(signal_id)},
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise EntityNotFoundError(f"{signal_id} aun no disponible")
        return _detail(row, self._clock.now())

    async def explain_signal(self, business_id: str, signal_id: str) -> SignalDetail:
        detail = await self.get_signal(business_id, signal_id)
        return SignalDetail(
            summary=detail.summary,
            gate_verdicts=detail.gate_verdicts,
            evidence=detail.evidence,
            narrative=detail.summary.cause.text,
        )

    async def list_anomalies(self, business_id: str, *, since: datetime) -> list[AnomalySummary]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        _SELECT_ANOMALIES, {"business_id": business_id, "since": since}
                    )
                )
                .mappings()
                .all()
            )
        return [
            AnomalySummary(
                anomaly_id=str(row["id"]),
                entity_ref=row["entity_ref"],
                method=row["method"].lower(),
                score=float(row["score"]),
                severity=row["severity"].lower(),
            )
            for row in rows
        ]

    async def get_pacing(self, business_id: str, entity_ref: str) -> PacingInfo:
        today = self._clock.now().date()
        month_start = today.replace(day=1)
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        _SELECT_PACING_INPUTS,
                        {
                            "business_id": business_id,
                            "entity_ref": entity_ref,
                            "month_start": month_start,
                            "today": today,
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise EntityNotFoundError(f"{entity_ref} aun no disponible")
            account_ref = row["account_ref"]
            policy = await SqlGuardrailRepository(session).find_for_account(account_ref=account_ref)
        currency = row["budget_currency"] or "EUR"
        if policy is None:
            zero = Money(Decimal(0), currency)
            return PacingInfo(entity_ref, 0.0, zero, zero, zero)

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
        return PacingInfo(
            entity_ref=entity_ref,
            pace_index=round(index, 3),
            projected=_money(projection_minor, currency),
            remaining=_money(remaining_minor, currency),
            new_daily=_money(round(remaining_minor / days_left), currency),
        )


def _days_in_month(today: date) -> int:
    next_month = today.replace(day=28) + timedelta(days=4)
    return (next_month.replace(day=1) - timedelta(days=1)).day


def _to_major(minor: int | None) -> Decimal:
    if minor is None:
        return Decimal(0)
    return Decimal(minor) / _MINOR_UNITS_PER_MAJOR


def _money(minor: int | None, currency: str) -> Money:
    return Money(_to_major(minor), currency)


def _summary(row: RowMapping, now: datetime) -> SignalSummary:
    return SignalSummary(
        signal_id=str(row["id"]),
        entity_ref=row["entity_ref"],
        kind=SignalKind(row["kind"].lower()),
        strength=int(row["strength"]),
        cause=Cause(text=row["cause"], signal_id=str(row["id"]), rule_id=row["rule_code"]),
        money_at_stake=_money(row["money_at_stake_minor"], row["money_at_stake_currency"]),
        window_label=row["data_window"],
        outcome=_derive_outcome(row["kind"], row["emitted_at"], row["outcome_at_14d"], now),
    )


def _detail(row: RowMapping, now: datetime) -> SignalDetail:
    summary = _summary(row, now)
    gate_verdicts = [
        GateVerdict(gate_name=v["gate"], passed=v["passed"], reason=v["reason"])
        for v in (row["gate_verdicts"] or [])
    ]
    evidence_raw = row["evidence"]
    evidence = (
        [
            Evidence(
                metric=evidence_raw["metric"],
                actual=evidence_raw["actual"],
                target=evidence_raw["target"],
                window_label=evidence_raw.get("span", summary.window_label),
            )
        ]
        if evidence_raw
        else []
    )
    return SignalDetail(
        summary=summary, gate_verdicts=gate_verdicts, evidence=evidence, narrative=""
    )


def _derive_outcome(
    kind: str, emitted_at: datetime, outcome_at_14d: object, now: datetime
) -> SignalOutcome:
    if kind in _NOT_APPLICABLE_KINDS:
        return SignalOutcome(
            status=SignalOutcomeStatus.NOT_APPLICABLE, days_remaining=None, evaluated_at=None
        )
    if outcome_at_14d:
        status_raw = outcome_at_14d.get("status") if isinstance(outcome_at_14d, dict) else None
        if status_raw in ("confirmed", "not_confirmed"):
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
    return SignalOutcome(status=SignalOutcomeStatus.PENDING, days_remaining=None, evaluated_at=None)
