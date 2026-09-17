"""Puertos de `opportunities` (tasks.md T113/T114). Todos son
anticorrupcion hacia `catalog`/`accounts`/`economics`/`proposals`: este
contexto no importa sus tipos de dominio, solo las primitivas que declara
aqui (mismo patron que `metrics.application.ports` para T116)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from safent_ads.opportunities.domain.campaign_brief import CampaignBrief
from safent_ads.proposals.domain.money import Money
from safent_ads.shared.ids import BusinessId, EntityRef, PlatformCode


@dataclass(frozen=True, kw_only=True, slots=True)
class CalendarEventGap:
    """Un hito de calendario abierto o proximo, para una cuenta de
    plataforma activa del negocio -- candidato a oportunidad hasta que se
    demuestre que ya tiene cobertura (tasks.md T113)."""

    calendar_event_id: str
    offering_id: str
    offering_name: str
    account_ref: EntityRef
    window_end: date
    region: str | None


class CalendarEventGapPort(Protocol):
    """Puerto hacia `catalog`/`accounts`: hitos de calendario dentro del
    horizonte de deteccion, cruzados con las cuentas de plataforma activas
    del negocio (una fila por combinacion hito x cuenta)."""

    async def list_gaps(
        self, *, business_id: BusinessId, today: date, horizon_days: int
    ) -> tuple[CalendarEventGap, ...]: ...


class OfferingContributionPort(Protocol):
    """Puerto hacia `economics`: contribucion esperada de invertir
    `daily_budget` en `offering_id` durante `duration_days`, a partir del
    perfil de economia unitaria YA calculado (`get_unit_economics`,
    T156) -- `None` si el negocio no tiene perfil todavia (oferta sin
    historial: ninguna subida de gasto sin medicion, profitability-
    engine.md §1)."""

    async def estimate_contribution_delta(
        self,
        *,
        business_id: BusinessId,
        offering_id: str,
        daily_budget: Money,
        duration_days: int,
    ) -> Money | None: ...


class DailyCandidateBudgetPort(Protocol):
    """Puerto hacia `proposals`: cuantos candidatos NUEVOS ya se aceptaron
    hoy para este negocio (NFR-11) -- reevaluar uno ya existente no cuenta
    de nuevo (no cambia su `created_at`)."""

    async def count_accepted_today(self, *, business_id: BusinessId, today: date) -> int: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class CampaignProposalOutcome:
    """Misma forma que el resto de `propose_*` del contrato MCP
    (contracts/mcp-tools.md §Escrituras: `{proposal_id, estado, diff_hash,
    expires_at, classification}`) -- T114 la reusa para `propose_campaign`."""

    proposal_id: str
    estado: str
    diff_hash: str
    expires_at: datetime
    classification: str


class CampaignProposalPort(Protocol):
    """Puerto de salida hacia `proposals` (N5, por encima de este contexto
    en el grafo): crea o actualiza la `Proposal` `CREATE_CAMPAIGN` de un
    candidato. `accept`/`defer` comparten el mismo `parameter` derivado de
    `candidate_key` para que FR-20 (una sola propuesta abierta por entidad
    y parametro) haga la deduplicacion -- este puerto no la reimplementa."""

    async def accept(
        self,
        *,
        business_id: BusinessId,
        account_ref: EntityRef,
        candidate_key: str,
        brief: CampaignBrief,
        expected_contribution_delta: Money | None,
        cause_sentence: str,
        now: datetime,
        proposed_by: str | None = None,
    ) -> CampaignProposalOutcome: ...

    async def defer(
        self,
        *,
        business_id: BusinessId,
        account_ref: EntityRef,
        candidate_key: str,
        brief: CampaignBrief,
        expected_contribution_delta: Money | None,
        cause_sentence: str,
        now: datetime,
        postpone_until: datetime,
    ) -> str | None: ...


# --- T114: `list_opportunities` (MCP, lectura) -------------------------------


@dataclass(frozen=True, kw_only=True, slots=True)
class OpenOpportunityView:
    """Proyeccion de lectura de una `Proposal` `CREATE_CAMPAIGN` todavia
    abierta (`pending`/`postponed`) -- decodifica su `CampaignBrief` para
    que `list_opportunities` (T114) no obligue al llamador a interpretar el
    JSON crudo del diff."""

    proposal_id: str
    state: str
    account_ref: EntityRef
    brief: CampaignBrief
    expected_contribution_delta: Money | None
    cause_sentence: str
    expires_at: datetime


class OpenOpportunityPort(Protocol):
    """Puerto hacia `proposals`: propuestas `CREATE_CAMPAIGN` abiertas de
    este contexto, ordenadas por `expected_contribution_delta DESC`
    (tool-surface.md §3: 'la cola cambia de eje')."""

    async def list_open(self, *, business_id: BusinessId) -> tuple[OpenOpportunityView, ...]: ...


# --- T114: validacion de `propose_campaign` ----------------------------------


class OfferingExistsPort(Protocol):
    """Puerto hacia `catalog`: si `offering_id` existe y esta activo para
    este negocio (T114: 'validated ... offering exists')."""

    async def exists(self, *, business_id: BusinessId, offering_id: str) -> bool: ...


class ActiveAccountLookupPort(Protocol):
    """Puerto hacia `accounts`: la cuenta `ACTIVE` de un negocio en una
    plataforma -- el ambito de la `Proposal` (`EntityLevel.ACCOUNT`, ver
    `opportunity_candidate.py`). `None` si no hay ninguna.

    Si hay mas de una cuenta `ACTIVE` (varias conexiones del negocio en esa
    plataforma, o varias cuentas remotas bajo la misma conexion) levanta
    `AmbiguousActiveAccountForPlatformError` -- nunca elige la primera en
    silencio (HANDOFF-ADS02-2026-09-11 "Seleccion ambigua")."""

    async def find_active_account(
        self,
        *,
        business_id: BusinessId,
        platform: PlatformCode,
        account_ref: EntityRef | None = None,
    ) -> EntityRef | None: ...


class AccountDailyCapPort(Protocol):
    """Puerto hacia `rules`: `daily_cap` del guardarrail de ambito
    `platform_account`, si ya esta configurado -- `None` cuando no hay fila
    todavia (T066: `config/caps.yaml`/guardarrailes reales dependen de
    entradas del propietario que aun no llegan para cuentas reales); en ese
    caso T114 no bloquea por presupuesto, el suelo del propio `CampaignBrief`
    (20 EUR/dia, FR-36 nace PAUSED) sigue siendo la red de seguridad."""

    async def get_daily_cap(self, *, account_ref: EntityRef) -> Money | None: ...
