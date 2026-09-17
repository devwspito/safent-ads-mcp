"""Puertos de `metrics` (plan.md §5: 'los puertos se declaran aqui').

`MetricFactRepository` persiste por clave natural con semantica UPSERT
(FR-5, NFR-6: reintentar nunca duplica un hecho).

Los tres puertos de `ReconcilePlatformVsCrm` (T116) al final del modulo son
anticorrupcion hacia `signals`/`crm`/`optimization`: `metrics` (N2) no
puede depender de ninguno de ellos en el grafo (plan.md §4), asi que solo
declara primitivas -- el adaptador concreto vive en `orchestration`
(transversal), mismo patron que `optimization.application.ports.
DueSignalOutcome`/`SignalResolutionPort` para T199."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from safent_ads.metrics.domain.freshness import Freshness
from safent_ads.metrics.domain.metric_fact import MetricFact
from safent_ads.metrics.domain.reconciliation import SignalContradicted
from safent_ads.metrics.domain.restatement import Restatement
from safent_ads.shared.ids import BusinessId, EntityRef


class MetricFactRepository(Protocol):
    """UPSERT por `(entity_ref, stat_date, stat_hour)` — nunca INSERT duplicado."""

    async def upsert_many(self, facts: Sequence[MetricFact]) -> None: ...

    async def find_by_natural_key(
        self, *, entity_ref: EntityRef, stat_date: date, stat_hour: int | None
    ) -> MetricFact | None: ...

    async def find_in_window(
        self, *, entity_ref: EntityRef, start_date: date, end_date: date
    ) -> Sequence[MetricFact]: ...

    async def latest_ingested_at(self, *, entity_ref: EntityRef) -> datetime | None: ...


class RestatementRepository(Protocol):
    """Solo-anexable (data-model.md §Restatement)."""

    async def record(self, restatement: Restatement) -> None: ...

    async def list_for_entity(self, *, entity_ref: EntityRef) -> Sequence[Restatement]: ...


class FreshnessRepository(Protocol):
    """Persiste el `Freshness` que `ComputeFreshness` calcula (NFR-1):
    contraparte de escritura de `execution.infrastructure.sql_freshness.
    SqlFreshnessPort`, que solo LEE `data_freshness`."""

    async def save(self, freshness: Freshness) -> None: ...


# --- T116: reconciliacion plataforma-vs-CRM ---------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class ActionableSignalRef:
    """Lo minimo que `ReconcilePlatformVsCrm` necesita de una senal
    accionable (`kind <> HOLD`) ya emitida y aun sin contradiccion resuelta.
    `window_start`/`window_end` son las de la evidencia que la disparo,
    ambas inclusive (mismo criterio que `MetricFactRepository.
    find_in_window`)."""

    signal_id: str
    entity_ref: EntityRef
    account_id: str
    rule_code: str
    window_start: date
    window_end: date


class ActionableSignalPort(Protocol):
    """Puerto hacia `signals`: senales accionables de un negocio, emitidas
    hasta `cutoff`, que todavia no tienen `contradicted_at`."""

    async def list_unresolved(
        self, *, business_id: BusinessId, cutoff: datetime
    ) -> tuple[ActionableSignalRef, ...]: ...


class CrmConversionsPort(Protocol):
    """Puerto hacia `crm`: conversiones de negocio confirmadas para una
    entidad en una ventana -- la verdad contra la que se reconcilia lo
    reportado por la plataforma. `window_end` inclusive, mismo criterio que
    `MetricFactRepository.find_in_window` (el adaptador ajusta el desfase
    contra `crm.LeadAttributionRepository.count_by_kind_in_window`, que es
    exclusivo)."""

    async def count_confirmed_conversions(
        self,
        *,
        business_id: BusinessId,
        entity_ref: EntityRef,
        window_start: date,
        window_end: date,
    ) -> int: ...


class SignalContradictionRecorder(Protocol):
    """Puerto de salida (T116): persiste la contradiccion -- marca
    `signals.contradicted_at` y alimenta el bucle de calibracion
    (`optimization`, T199) por su escritor YA EXISTENTE
    (`SqlSignalOutcomeRepository.record`), sin que `metrics` conozca ese
    contexto."""

    async def record(self, *, event: SignalContradicted) -> None: ...
