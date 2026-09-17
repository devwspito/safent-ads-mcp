"""Puertos de lectura hacia los demas contextos (plan.md §4: leidos, nunca
importados). Cada Protocol agrupa las herramientas de `contracts/mcp-tools.md`
que comparten el mismo origen de datos; la integracion cablea adaptadores
reales, `mcp/testing/fakes.py` cablea dobles para los tests de esta lane."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from safent_ads.mcp.application.dto import (
    AccountFreshness,
    AnomalySummary,
    BrandAssetSummary,
    BrandKitDetail,
    BusinessSummary,
    CalendarEventDetail,
    CalendarEventSummary,
    CampaignSummary,
    CreativeBriefSummary,
    CreativeDetail,
    CreativeJobStatus,
    CreativeSummary,
    DecisionLogEntryDetail,
    DecisionLogEntrySummary,
    EntitySummary,
    GaqlResult,
    GuardrailInfo,
    InsightsSnapshot,
    KillSwitchStatus,
    MetricsSeries,
    OfferingSummary,
    PacingInfo,
    Page,
    PlatformAccountSummary,
    PortfolioOverview,
    ProposalDetail,
    ProposalSummary,
    RuleDetail,
    RuleExplanation,
    RuleSummary,
    SignalDetail,
    SignalsPage,
    Window,
)


class BusinessDirectoryPort(Protocol):
    """Respalda `list_businesses`: la unica herramienta sin `business_id`
    en sus argumentos, filtrada por `CallerScope` en el propio puerto."""

    async def list_businesses(
        self, allowed_business_ids: frozenset[str]
    ) -> list[BusinessSummary]: ...


class PortfolioReadPort(Protocol):
    async def list_platform_accounts(self, business_id: str) -> list[PlatformAccountSummary]: ...

    async def get_portfolio_overview(
        self, business_id: str, window: Window
    ) -> PortfolioOverview: ...

    async def get_data_freshness(self, business_id: str) -> list[AccountFreshness]: ...


class EntityReadPort(Protocol):
    async def list_campaigns(
        self,
        business_id: str,
        *,
        platform: str | None,
        status: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[CampaignSummary]: ...

    async def get_campaign(self, business_id: str, entity_ref: str) -> CampaignSummary: ...

    async def list_children(
        self, business_id: str, entity_ref: str, *, limit: int, cursor: str | None
    ) -> Page[EntitySummary]: ...

    async def list_creatives(
        self, business_id: str, *, media_kind: str | None, limit: int, cursor: str | None
    ) -> Page[CreativeSummary]: ...

    async def get_creative(self, business_id: str, asset_id: str) -> CreativeDetail: ...

    async def get_entity_metrics(
        self, business_id: str, entity_ref: str, *, window: Window, granularity: str
    ) -> MetricsSeries: ...

    async def get_insights(
        self, business_id: str, entity_ref: str, *, window: Window, breakdown: str | None
    ) -> InsightsSnapshot: ...


class GaqlPort(Protocol):
    async def run_gaql(self, business_id: str, account_ref: str, query: str) -> GaqlResult: ...


class SignalReadPort(Protocol):
    async def list_signals(
        self,
        business_id: str,
        *,
        kind: str | None,
        min_strength: int | None,
        since: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> SignalsPage: ...

    async def get_signal(self, business_id: str, signal_id: str) -> SignalDetail: ...

    async def explain_signal(self, business_id: str, signal_id: str) -> SignalDetail: ...

    async def list_anomalies(
        self, business_id: str, *, since: datetime
    ) -> list[AnomalySummary]: ...

    async def get_pacing(self, business_id: str, entity_ref: str) -> PacingInfo: ...


class RuleReadPort(Protocol):
    async def list_rules(
        self, business_id: str, *, platform: str | None, enabled: bool | None
    ) -> list[RuleSummary]: ...

    async def get_rule(self, business_id: str, rule_id: str) -> RuleDetail: ...

    async def explain_rule(
        self, business_id: str, rule_id: str, *, entity_ref: str | None
    ) -> RuleExplanation: ...

    async def list_guardrails(self, business_id: str, scope_ref: str) -> list[GuardrailInfo]: ...

    async def get_kill_switch_status(self, business_id: str) -> KillSwitchStatus: ...


class ProposalReadPort(Protocol):
    async def list_proposals(
        self,
        business_id: str,
        *,
        state: str | None,
        cause_key: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[ProposalSummary]: ...

    async def get_proposal(self, business_id: str, proposal_id: str) -> ProposalDetail: ...


class CatalogReadPort(Protocol):
    async def list_offerings(self, business_id: str) -> list[OfferingSummary]: ...

    async def list_calendar_events(
        self, business_id: str, *, open_only: bool
    ) -> list[CalendarEventSummary]: ...

    async def get_calendar_event(
        self, business_id: str, calendar_event_id: str
    ) -> CalendarEventDetail: ...


class AuditReadPort(Protocol):
    async def search_decision_log(
        self,
        business_id: str,
        *,
        since: datetime,
        until: datetime,
        event_type: str | None,
        entity_ref: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[DecisionLogEntrySummary]: ...

    async def get_decision_log_entry(
        self, business_id: str, seq: int
    ) -> DecisionLogEntryDetail: ...


class CreativeReadPort(Protocol):
    async def list_creative_briefs(self, business_id: str) -> list[CreativeBriefSummary]: ...

    async def get_creative_job(self, business_id: str, job_id: str) -> CreativeJobStatus: ...


class BrandReadPort(Protocol):
    """Respalda `get_brand_kit`/`list_brand_assets` y el bloque de marca de
    `get_project_context`. `get_brand_kit` lanza `EntityNotFoundError` si el
    negocio aun no tiene kit -- ausencia de marca es un estado valido y
    frecuente (el propietario todavia no ha completado
    `config/brand/<business>.yaml`), no un fallo del puerto."""

    async def get_brand_kit(self, business_id: str) -> BrandKitDetail: ...

    async def list_brand_assets(
        self, business_id: str, *, kind: str | None
    ) -> list[BrandAssetSummary]: ...


class CapabilityReadPort(Protocol):
    """Respalda `get_capabilities`: unicamente la sonda que ningun otro
    puerto ya cubre (claves BYOK de generacion, tool-surface.md §6). El
    resto de `get_capabilities` (escrituras de plataforma, autonomia,
    credenciales de cuenta) se deriva de `RuleReadPort`/`PortfolioReadPort`,
    ya existentes -- no se duplica aqui."""

    async def configured_byok_keys(self) -> frozenset[str]: ...
