"""Repositorios SQL de `signals` sobre `signals` y `anomalies` (migraciones
0006 y 0012), mas los dos adaptadores que leen hechos de `metrics_daily`:
`SqlMetricWindowRepository` (la capa anticorrupcion que el lane de senales
dejo pendiente) y `SqlDailySpendSeriesRepository`.

Decisiones de mapeo:

- `signals` y `anomalies` guardan `business_id` y `cycle_id`, que el dominio
  no lleva: el negocio se resuelve desde `ad_entities` (su dueno) y el ciclo
  lo inyecta quien construye el repositorio, porque es propiedad de la
  ejecucion, no de la senal (plan.md §7: cada ciclo lleva su `cycle_id`).
- Reemitir la misma senal dentro del mismo ciclo actualiza la fila en vez de
  duplicarla: es la UNIQUE `(cycle_id, entity_ref, kind)` del esquema, y deja
  el ciclo repetible sin efectos.
- `Signal` solo conoce su `span`; el esquema quiere las fechas de la ventana.
  Se derivan de `emitted_at` hacia atras, que es lo que significa "ultimos N
  dias incluido hoy".
- La cuota de impresiones perdida se guarda como fraccion 0-1 (asi la reporta
  Google) y el dominio la usa en puntos porcentuales.
- `MetricWindow` agrega solo la tabla diaria: la horaria vive 14 dias y las
  ventanas del catalogo llegan a 30.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from safent_ads.metrics.application.ports import ActionableSignalRef
from safent_ads.shared.ids import BusinessId, EntityRef
from safent_ads.signals.domain.anomaly import Anomaly, AnomalyMethod, AnomalySeverity
from safent_ads.signals.domain.cause import Cause
from safent_ads.signals.domain.gate_verdict import GateName, GateVerdict
from safent_ads.signals.domain.metric_window import MetricWindow
from safent_ads.signals.domain.signal import CreativeSignal, CreativeSignalKind, Signal, SignalKind
from safent_ads.signals.domain.value_objects import Evidence, MoneyAtStake, SignalStrength
from safent_ads.signals.domain.window_span import WindowSpan
from safent_ads.signals.infrastructure.errors import UnknownEntityRefError

__all__ = [
    "SqlAnomalyRepository",
    "SqlCreativeSignalRepository",
    "SqlDailySpendSeriesRepository",
    "SqlMetricWindowRepository",
    "SqlSignalReconciliationRepository",
    "SqlSignalRepository",
]

_PERCENT: Final = 100
_SPAN_DAYS: Final[dict[WindowSpan, int]] = {
    WindowSpan.D3: 3,
    WindowSpan.D7: 7,
    WindowSpan.D14: 14,
    WindowSpan.D30: 30,
}
# El detector de anomalias solo se alimenta hoy de la serie de gasto
# (`DailySpendSeriesRepository`); la fila lo deja escrito en vez de fingir
# que la metrica es desconocida.
_ANOMALY_METRIC: Final = "spend_minor"

_UPSERT_SIGNAL: Final = """
    INSERT INTO signals (business_id, entity_ref, kind, strength, cause, cause_code, rule_code,
                         gate_reason, gate_verdicts, evidence, data_window, window_start,
                         window_end, money_at_stake_minor, money_at_stake_currency, cycle_id,
                         emitted_at)
    SELECT entity.business_id, :entity_ref, :kind, :strength, :cause, :cause_code, :rule_code,
           :gate_reason, CAST(:gate_verdicts AS jsonb), CAST(:evidence AS jsonb),
           :data_window, :window_start,
           :window_end, :money_at_stake_minor, :money_at_stake_currency, :cycle_id, :emitted_at
      FROM ad_entities AS entity
     WHERE entity.entity_ref = :entity_ref
    ON CONFLICT (cycle_id, entity_ref, kind) DO UPDATE
        SET strength               = EXCLUDED.strength,
            cause                  = EXCLUDED.cause,
            cause_code             = EXCLUDED.cause_code,
            rule_code              = EXCLUDED.rule_code,
            gate_reason            = EXCLUDED.gate_reason,
            gate_verdicts          = EXCLUDED.gate_verdicts,
            evidence               = EXCLUDED.evidence,
            data_window            = EXCLUDED.data_window,
            window_start           = EXCLUDED.window_start,
            window_end             = EXCLUDED.window_end,
            money_at_stake_minor   = EXCLUDED.money_at_stake_minor,
            money_at_stake_currency = EXCLUDED.money_at_stake_currency,
            emitted_at             = EXCLUDED.emitted_at
    RETURNING id
"""

_SELECT_SIGNAL: Final = """
    SELECT id, entity_ref, kind, strength, cause, cause_code, rule_code, gate_reason,
           gate_verdicts, evidence, data_window, money_at_stake_minor,
           money_at_stake_currency, emitted_at
      FROM signals
     WHERE entity_ref = :entity_ref AND kind = ANY(:kinds)
     ORDER BY emitted_at DESC, id DESC
     LIMIT 1
"""

_UPSERT_ANOMALY: Final = """
    INSERT INTO anomalies (business_id, entity_ref, method, metric, score, severity,
                           interval_start, evidence, cycle_id, detected_at)
    SELECT entity.business_id, :entity_ref, :method, :metric, :score, :severity,
           :interval_start, '{}'::jsonb, :cycle_id, :detected_at
      FROM ad_entities AS entity
     WHERE entity.entity_ref = :entity_ref
    ON CONFLICT (entity_ref, method, metric, interval_start) DO UPDATE
        SET score       = EXCLUDED.score,
            severity    = EXCLUDED.severity,
            detected_at = EXCLUDED.detected_at
    RETURNING id
"""

_LIST_UNRESOLVED_ACTIONABLE: Final = """
    SELECT s.id AS signal_id, s.entity_ref, e.platform_account_id AS account_id,
           s.rule_code, s.window_start, s.window_end
      FROM signals AS s
      JOIN ad_entities AS e ON e.business_id = s.business_id AND e.entity_ref = s.entity_ref
     WHERE s.business_id = :business_id
       AND s.kind <> 'HOLD'
       AND s.emitted_at <= :cutoff
       AND s.contradicted_at IS NULL
     ORDER BY s.emitted_at
"""

_MARK_CONTRADICTED: Final = """
    UPDATE signals SET contradicted_at = :contradicted_at
     WHERE id = :signal_id AND contradicted_at IS NULL
"""

_AGGREGATE_WINDOW: Final = """
    SELECT COALESCE(sum(spend), 0)            AS spend_minor,
           COALESCE(sum(impressions), 0)      AS impressions,
           COALESCE(sum(clicks), 0)           AS clicks,
           COALESCE(sum(reach), 0)            AS reach,
           COALESCE(sum(conversions_lead + conversions_whatsapp
                        + conversions_call + conversions_business_conversion), 0) AS conversions,
           COALESCE(sum(conversion_value), 0) AS conversion_value_minor,
           COALESCE(sum(video_views_3s), 0)   AS video_views_3s,
           COALESCE(sum(video_views_75pct), 0) AS video_views_75pct,
           sum(search_lost_is_budget * impressions)
               FILTER (WHERE search_lost_is_budget IS NOT NULL)
             / NULLIF(sum(impressions) FILTER (WHERE search_lost_is_budget IS NOT NULL), 0)
                                              AS search_lost_is_budget
      FROM metrics_daily
     WHERE entity_ref = :entity_ref AND stat_date BETWEEN :start AND :end
"""


class SqlSignalRepository:
    """`SignalRepository` (signals/application/ports.py)."""

    def __init__(self, session: AsyncSession, *, cycle_id: UUID) -> None:
        self._session = session
        self._cycle_id = cycle_id

    async def save(self, signal: Signal) -> None:
        await _save_signal(self._session, signal, cycle_id=self._cycle_id)

    async def find_latest_for_entity(self, *, entity_ref: EntityRef) -> Signal | None:
        row = await _latest_row(self._session, entity_ref, [k.value.upper() for k in SignalKind])
        if row is None:
            return None
        return Signal(**_common_signal_fields(row), kind=SignalKind(row["kind"].lower()))


class SqlCreativeSignalRepository:
    """`CreativeSignalRepository`: misma tabla, `kind` de creatividad
    (data-model.md §Signal: `SignalKind` o `CreativeSignalKind`)."""

    def __init__(self, session: AsyncSession, *, cycle_id: UUID) -> None:
        self._session = session
        self._cycle_id = cycle_id

    async def save(self, signal: CreativeSignal) -> None:
        await _save_signal(self._session, signal, cycle_id=self._cycle_id)

    async def find_latest_for_entity(self, *, entity_ref: EntityRef) -> CreativeSignal | None:
        row = await _latest_row(
            self._session, entity_ref, [k.value.upper() for k in CreativeSignalKind]
        )
        if row is None:
            return None
        return CreativeSignal(
            **_common_signal_fields(row), kind=CreativeSignalKind(row["kind"].lower())
        )


class SqlAnomalyRepository:
    """`AnomalyRepository`: solo notifica, nunca acciona (data-model.md)."""

    def __init__(self, session: AsyncSession, *, cycle_id: UUID) -> None:
        self._session = session
        self._cycle_id = cycle_id

    async def save(self, anomaly: Anomaly) -> None:
        result = await self._session.execute(
            text(_UPSERT_ANOMALY),
            {
                "entity_ref": str(anomaly.entity_ref),
                "method": anomaly.method.value,
                "metric": _ANOMALY_METRIC,
                "score": anomaly.score,
                "severity": anomaly.severity.value.upper(),
                "interval_start": anomaly.detected_at,
                "cycle_id": self._cycle_id,
                "detected_at": anomaly.detected_at,
            },
        )
        if result.first() is None:
            raise UnknownEntityRefError(
                f"{anomaly.entity_ref} no existe en ad_entities: no se anota su anomalia"
            )
        await self._session.flush()

    async def list_for_entity(self, *, entity_ref: EntityRef) -> Sequence[Anomaly]:
        result = await self._session.execute(
            text(
                """
                SELECT entity_ref, method, score, severity, detected_at
                  FROM anomalies
                 WHERE entity_ref = :entity_ref
                 ORDER BY detected_at, id
                """
            ),
            {"entity_ref": str(entity_ref)},
        )
        return [
            Anomaly(
                entity_ref=EntityRef.parse(row["entity_ref"]),
                method=AnomalyMethod(row["method"]),
                score=float(row["score"]),
                severity=AnomalySeverity(row["severity"].lower()),
                detected_at=row["detected_at"],
            )
            for row in result.mappings()
        ]


class SqlSignalReconciliationRepository:
    """Adaptador de `metrics.application.ports.ActionableSignalPort` (T116):
    `signals` (N3) puede depender de `metrics` (N2) en el grafo -- lo
    contrario no (plan.md §4) -- asi que este adaptador vive aqui, no en
    `metrics`, y solo expone las primitivas que ese puerto declara, nunca
    el agregado `Signal`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_unresolved(
        self, *, business_id: BusinessId, cutoff: datetime
    ) -> tuple[ActionableSignalRef, ...]:
        rows = (
            await self._session.execute(
                text(_LIST_UNRESOLVED_ACTIONABLE),
                {"business_id": business_id.value, "cutoff": cutoff},
            )
        ).mappings()
        return tuple(
            ActionableSignalRef(
                signal_id=str(row["signal_id"]),
                entity_ref=EntityRef.parse(row["entity_ref"]),
                account_id=str(row["account_id"]),
                rule_code=row["rule_code"],
                window_start=row["window_start"],
                window_end=row["window_end"],
            )
            for row in rows
        )

    async def mark_contradicted(self, *, signal_id: str, contradicted_at: datetime) -> None:
        await self._session.execute(
            text(_MARK_CONTRADICTED), {"signal_id": signal_id, "contradicted_at": contradicted_at}
        )
        await self._session.flush()


class SqlMetricWindowRepository:
    """`MetricWindowRepository`: traduce hechos de `metrics` al DTO propio de
    `signals` (plan.md §4: `signals` no depende de `metrics`). Esta clase es
    la unica que conoce las dos formas."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_window(
        self, *, entity_ref: EntityRef, span: WindowSpan, as_of: datetime
    ) -> MetricWindow:
        end_date = as_of.date()
        start_date = end_date - timedelta(days=_SPAN_DAYS[span] - 1)
        result = await self._session.execute(
            text(_AGGREGATE_WINDOW),
            {"entity_ref": str(entity_ref), "start": start_date, "end": end_date},
        )
        row = result.mappings().one()
        lost_is_budget = row["search_lost_is_budget"]
        return MetricWindow(
            entity_ref=entity_ref,
            span=span,
            spend_minor=int(row["spend_minor"]),
            impressions=int(row["impressions"]),
            clicks=int(row["clicks"]),
            reach=int(row["reach"]),
            conversions=int(row["conversions"]),
            conversion_value_minor=int(row["conversion_value_minor"]),
            video_views_3s=int(row["video_views_3s"]),
            video_views_75pct=int(row["video_views_75pct"]),
            search_lost_is_budget_pct=(
                None if lost_is_budget is None else float(Decimal(lost_is_budget) * _PERCENT)
            ),
        )


def _postgres_weekday(day: date) -> int:
    """`EXTRACT(DOW ...)` cuenta 0=domingo; Python, 1=lunes .. 7=domingo. El
    dia se calcula aqui y no en SQL: `:as_of::date` deja el parametro sin
    enlazar (SQLAlchemy no reconoce un nombre seguido de `::`) y Postgres
    recibia dos puntos sueltos."""
    return day.isoweekday() % 7


class SqlDailySpendSeriesRepository:
    """`DailySpendSeriesRepository`: series crudas para `anomaly.py`, que
    necesita los puntos individuales y no un agregado."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch_same_weekday_series(
        self, *, entity_ref: EntityRef, as_of: date, weeks: int
    ) -> Sequence[float]:
        result = await self._session.execute(
            text(
                """
                SELECT spend FROM metrics_daily
                 WHERE entity_ref = :entity_ref
                   AND stat_date < :as_of
                   AND EXTRACT(DOW FROM stat_date) = :weekday
                 ORDER BY stat_date DESC
                 LIMIT :weeks
                """
            ),
            {
                "entity_ref": str(entity_ref),
                "as_of": as_of,
                "weekday": _postgres_weekday(as_of),
                "weeks": weeks,
            },
        )
        return [float(value) for value in reversed(result.scalars().all())]

    async def fetch_today_value(self, *, entity_ref: EntityRef, as_of: date) -> float:
        result = await self._session.execute(
            text(
                """
                SELECT spend FROM metrics_daily
                 WHERE entity_ref = :entity_ref AND stat_date = :as_of
                """
            ),
            {"entity_ref": str(entity_ref), "as_of": as_of},
        )
        value = result.scalar_one_or_none()
        return 0.0 if value is None else float(value)


async def _save_signal(
    session: AsyncSession, signal: Signal | CreativeSignal, *, cycle_id: UUID
) -> None:
    result = await session.execute(text(_UPSERT_SIGNAL), _signal_params(signal, cycle_id))
    if result.first() is None:
        raise UnknownEntityRefError(
            f"{signal.entity_ref} no existe en ad_entities: no se guarda su senal"
        )
    await session.flush()


async def _latest_row(
    session: AsyncSession, entity_ref: EntityRef, kinds: Sequence[str]
) -> RowMapping | None:
    result = await session.execute(
        text(_SELECT_SIGNAL), {"entity_ref": str(entity_ref), "kinds": list(kinds)}
    )
    return result.mappings().one_or_none()


def _signal_params(signal: Signal | CreativeSignal, cycle_id: UUID) -> Mapping[str, Any]:
    window_end = signal.emitted_at.date()
    window_start = window_end - timedelta(days=_SPAN_DAYS[signal.span] - 1)
    blocked = [verdict for verdict in signal.gate_verdicts if not verdict.passed]
    return {
        "entity_ref": str(signal.entity_ref),
        "kind": signal.kind.value.upper(),
        "strength": signal.strength.value,
        "cause": signal.cause_sentence,
        "cause_code": signal.cause.value,
        "rule_code": signal.rule_code,
        "gate_reason": _gate_reason(signal, blocked),
        "gate_verdicts": _encode_verdicts(signal.gate_verdicts),
        "evidence": _encode_evidence(signal.evidence),
        "data_window": signal.span.value,
        "window_start": window_start,
        "window_end": window_end,
        "money_at_stake_minor": signal.money_at_stake.minor_units,
        "money_at_stake_currency": signal.money_at_stake.currency,
        "cycle_id": cycle_id,
        "emitted_at": signal.emitted_at,
    }


def _gate_reason(signal: Signal | CreativeSignal, blocked: Sequence[GateVerdict]) -> str | None:
    """`signals_hold_needs_reason` (0006_signals.py) exige `gate_reason` no
    nulo en TODO `HOLD`, no solo el bloqueado por una puerta:
    `signal_engine.hold_signal` tambien emite HOLD cuando todas las puertas
    pasan y ninguna condicion del catalogo dispara (`Cause.INSIDE_TARGET_BAND`)
    -- ese caso no tenia ningun `GateVerdict` bloqueado, asi que caia a
    `None` y violaba el CHECK la primera vez que se persistia contra
    Postgres real. `cause_sentence` ya lleva la frase humana de por que se
    mantiene, asi que sirve de motivo."""
    if blocked:
        return blocked[0].reason
    if isinstance(signal, Signal) and signal.kind is SignalKind.HOLD:
        return signal.cause_sentence
    return None


def _encode_verdicts(verdicts: Sequence[GateVerdict]) -> str:
    return json.dumps(
        [
            {"gate": verdict.gate.value, "passed": verdict.passed, "reason": verdict.reason}
            for verdict in verdicts
        ]
    )


def _encode_evidence(evidence: Evidence) -> str:
    return json.dumps(
        {
            "metric": evidence.metric,
            "actual": evidence.actual,
            "target": evidence.target,
            "baseline": evidence.baseline,
            "span": evidence.span.value,
        }
    )


def _common_signal_fields(row: RowMapping) -> Mapping[str, Any]:
    evidence = row["evidence"]
    return {
        "signal_id": str(row["id"]),
        "entity_ref": EntityRef.parse(row["entity_ref"]),
        "strength": SignalStrength(int(row["strength"])),
        "cause": Cause(row["cause_code"]),
        "cause_sentence": row["cause"],
        "span": WindowSpan(row["data_window"]),
        "money_at_stake": MoneyAtStake(
            minor_units=int(row["money_at_stake_minor"]),
            currency=row["money_at_stake_currency"],
        ),
        "evidence": Evidence(
            metric=evidence["metric"],
            actual=evidence["actual"],
            target=evidence["target"],
            baseline=evidence["baseline"],
            span=WindowSpan(evidence["span"]),
        ),
        "gate_verdicts": tuple(
            GateVerdict(
                gate=GateName(verdict["gate"]),
                passed=verdict["passed"],
                reason=verdict["reason"],
            )
            for verdict in row["gate_verdicts"]
        ),
        "emitted_at": row["emitted_at"],
        "rule_code": row["rule_code"],
    }
