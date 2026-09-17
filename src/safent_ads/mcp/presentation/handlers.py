"""Handlers de las 32 herramientas de lectura (T045): traducen argumentos
validados a llamadas de puerto y devuelven el DTO tal cual (el dispatcher
serializa). Ninguno contiene logica de negocio — eso vive en el contexto
dueno del dato, al otro lado del puerto."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from safent_ads.mcp.application.caller_scope import CallerScope
from safent_ads.mcp.application.dto import (
    AccountFreshness,
    AnomalySummary,
    BrandAssetSummary,
    BrandKitDetail,
    BusinessSummary,
    CalendarEventDetail,
    CalendarEventSummary,
    CampaignSummary,
    CapabilitiesReport,
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
)
from safent_ads.mcp.application.get_capabilities import GetCapabilities
from safent_ads.mcp.application.get_project_context import GetProjectContext
from safent_ads.mcp.presentation import args as a
from safent_ads.mcp.presentation.converters import page_params, to_window
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.shared.clock import Clock

type Handler[ArgsT, ReturnT] = Callable[[ArgsT, CallerScope], Awaitable[ReturnT]]


def build_handlers(ports: ReadModelPorts, clock: Clock) -> dict[str, Any]:
    """Devuelve `{tool_name: handler}`; `catalog.py` lo consume para armar
    `ToolDefinition`. Una funcion en vez de 32 constantes de modulo para
    tener un unico punto que cierra sobre `ports`.

    Tipado `Any` a proposito en el valor del diccionario: cada `_x(ports)`
    de abajo si tiene su tipo completo y preciso
    (`Handler[XArgs, Y]`), pero un `dict`
    homogeneo no puede conservar 32 firmas distintas a la vez. La erosion
    de tipo ocurre solo en este punto de ensamblado — igual que
    `ToolRegistry` ya almacena `ToolDefinition[Any]` internamente — y no
    dentro de ningun handler."""
    return {
        "list_businesses": _list_businesses(ports),
        "list_platform_accounts": _list_platform_accounts(ports),
        "get_portfolio_overview": _get_portfolio_overview(ports),
        "get_data_freshness": _get_data_freshness(ports),
        "list_campaigns": _list_campaigns(ports),
        "get_campaign": _get_campaign(ports),
        "list_ad_sets": _list_children(ports),
        "list_ads": _list_children(ports),
        "list_creatives": _list_creatives(ports),
        "get_creative": _get_creative(ports),
        "get_entity_metrics": _get_entity_metrics(ports),
        "get_insights": _get_insights(ports),
        "run_gaql": _run_gaql(ports),
        "list_signals": _list_signals(ports),
        "get_signal": _get_signal(ports),
        "explain_signal": _explain_signal(ports),
        "list_anomalies": _list_anomalies(ports),
        "get_pacing": _get_pacing(ports),
        "list_rules": _list_rules(ports),
        "get_rule": _get_rule(ports),
        "explain_rule": _explain_rule(ports),
        "list_guardrails": _list_guardrails(ports),
        "get_kill_switch_status": _get_kill_switch_status(ports),
        "list_proposals": _list_proposals(ports),
        "get_proposal": _get_proposal(ports),
        "list_offerings": _list_offerings(ports),
        "list_calendar_events": _list_calendar_events(ports),
        "get_calendar_event": _get_calendar_event(ports),
        "search_decision_log": _search_decision_log(ports),
        "get_decision_log_entry": _get_decision_log_entry(ports),
        "list_creative_briefs": _list_creative_briefs(ports),
        "get_creative_job": _get_creative_job(ports),
        "get_brand_kit": _get_brand_kit(ports),
        "list_brand_assets": _list_brand_assets(ports),
        "get_project_context": _get_project_context(ports, clock),
        "get_capabilities": _get_capabilities(ports, clock),
    }


# --- portfolio ----------------------------------------------------------


def _list_businesses(
    ports: ReadModelPorts,
) -> Handler[a.ListBusinessesArgs, list[BusinessSummary]]:
    async def handler(
        _args: a.ListBusinessesArgs, caller_scope: CallerScope
    ) -> list[BusinessSummary]:
        return await ports.business_directory.list_businesses(caller_scope.allowed_business_ids)

    return handler


def _list_platform_accounts(
    ports: ReadModelPorts,
) -> Handler[a.ListPlatformAccountsArgs, list[PlatformAccountSummary]]:
    async def handler(
        args: a.ListPlatformAccountsArgs, _caller_scope: CallerScope
    ) -> list[PlatformAccountSummary]:
        return await ports.portfolio.list_platform_accounts(args.business_id)

    return handler


def _get_portfolio_overview(
    ports: ReadModelPorts,
) -> Handler[a.GetPortfolioOverviewArgs, PortfolioOverview]:
    async def handler(
        args: a.GetPortfolioOverviewArgs, _caller_scope: CallerScope
    ) -> PortfolioOverview:
        return await ports.portfolio.get_portfolio_overview(
            args.business_id, to_window(args.window)
        )

    return handler


def _get_data_freshness(
    ports: ReadModelPorts,
) -> Handler[a.GetDataFreshnessArgs, list[AccountFreshness]]:
    async def handler(
        args: a.GetDataFreshnessArgs, _caller_scope: CallerScope
    ) -> list[AccountFreshness]:
        return await ports.portfolio.get_data_freshness(args.business_id)

    return handler


# --- entities / metrics ---------------------------------------------------


def _list_campaigns(ports: ReadModelPorts) -> Handler[a.ListCampaignsArgs, Page[CampaignSummary]]:
    async def handler(
        args: a.ListCampaignsArgs, _caller_scope: CallerScope
    ) -> Page[CampaignSummary]:
        limit, cursor = page_params(args.page)
        platform = args.platform.value if args.platform else None
        status = args.status.value if args.status else None
        return await ports.entity.list_campaigns(
            args.business_id, platform=platform, status=status, limit=limit, cursor=cursor
        )

    return handler


def _get_campaign(ports: ReadModelPorts) -> Handler[a.GetCampaignArgs, CampaignSummary]:
    async def handler(args: a.GetCampaignArgs, _caller_scope: CallerScope) -> CampaignSummary:
        return await ports.entity.get_campaign(args.business_id, args.entity_ref)

    return handler


def _list_children(
    ports: ReadModelPorts,
) -> Handler[a.ListAdSetsArgs | a.ListAdsArgs, Page[EntitySummary]]:
    """Comparte implementacion entre `list_ad_sets` y `list_ads`: ambos
    piden "el siguiente nivel de la jerarquia" del mismo `AdEntity`
    (plan.md §5: "mismo tipo, `EntityLevel` discrimina")."""

    async def handler(
        args: a.ListAdSetsArgs | a.ListAdsArgs, _caller_scope: CallerScope
    ) -> Page[EntitySummary]:
        limit, cursor = page_params(args.page)
        return await ports.entity.list_children(
            args.business_id, args.entity_ref, limit=limit, cursor=cursor
        )

    return handler


def _list_creatives(
    ports: ReadModelPorts,
) -> Handler[a.ListCreativesArgs, Page[CreativeSummary]]:
    async def handler(
        args: a.ListCreativesArgs, _caller_scope: CallerScope
    ) -> Page[CreativeSummary]:
        limit, cursor = page_params(args.page)
        media_kind = args.media_kind.value if args.media_kind else None
        return await ports.entity.list_creatives(
            args.business_id, media_kind=media_kind, limit=limit, cursor=cursor
        )

    return handler


def _get_creative(ports: ReadModelPorts) -> Handler[a.GetCreativeArgs, CreativeDetail]:
    async def handler(args: a.GetCreativeArgs, _caller_scope: CallerScope) -> CreativeDetail:
        return await ports.entity.get_creative(args.business_id, args.asset_id)

    return handler


def _get_entity_metrics(ports: ReadModelPorts) -> Handler[a.GetEntityMetricsArgs, MetricsSeries]:
    async def handler(args: a.GetEntityMetricsArgs, _caller_scope: CallerScope) -> MetricsSeries:
        return await ports.entity.get_entity_metrics(
            args.business_id,
            args.entity_ref,
            window=to_window(args.window),
            granularity=args.granularity.value,
        )

    return handler


def _get_insights(ports: ReadModelPorts) -> Handler[a.GetInsightsArgs, InsightsSnapshot]:
    async def handler(args: a.GetInsightsArgs, _caller_scope: CallerScope) -> InsightsSnapshot:
        return await ports.entity.get_insights(
            args.business_id,
            args.entity_ref,
            window=to_window(args.window),
            breakdown=args.breakdown,
        )

    return handler


def _run_gaql(ports: ReadModelPorts) -> Handler[a.RunGaqlArgs, GaqlResult]:
    async def handler(args: a.RunGaqlArgs, _caller_scope: CallerScope) -> GaqlResult:
        return await ports.gaql.run_gaql(args.business_id, args.account_ref, args.query)

    return handler


# --- signals / anomalies / pacing ----------------------------------------


def _list_signals(ports: ReadModelPorts) -> Handler[a.ListSignalsArgs, SignalsPage]:
    async def handler(args: a.ListSignalsArgs, _caller_scope: CallerScope) -> SignalsPage:
        limit, cursor = page_params(args.page)
        kind = args.kind.value if args.kind else None
        return await ports.signal.list_signals(
            args.business_id,
            kind=kind,
            min_strength=args.min_strength,
            since=args.since,
            limit=limit,
            cursor=cursor,
        )

    return handler


def _get_signal(ports: ReadModelPorts) -> Handler[a.GetSignalArgs, SignalDetail]:
    async def handler(args: a.GetSignalArgs, _caller_scope: CallerScope) -> SignalDetail:
        return await ports.signal.get_signal(args.business_id, args.signal_id)

    return handler


def _explain_signal(ports: ReadModelPorts) -> Handler[a.ExplainSignalArgs, SignalDetail]:
    async def handler(args: a.ExplainSignalArgs, _caller_scope: CallerScope) -> SignalDetail:
        return await ports.signal.explain_signal(args.business_id, args.signal_id)

    return handler


def _list_anomalies(ports: ReadModelPorts) -> Handler[a.ListAnomaliesArgs, list[AnomalySummary]]:
    async def handler(
        args: a.ListAnomaliesArgs, _caller_scope: CallerScope
    ) -> list[AnomalySummary]:
        return await ports.signal.list_anomalies(args.business_id, since=args.since)

    return handler


def _get_pacing(ports: ReadModelPorts) -> Handler[a.GetPacingArgs, PacingInfo]:
    async def handler(args: a.GetPacingArgs, _caller_scope: CallerScope) -> PacingInfo:
        return await ports.signal.get_pacing(args.business_id, args.entity_ref)

    return handler


# --- rules / guardrails ---------------------------------------------------


def _list_rules(ports: ReadModelPorts) -> Handler[a.ListRulesArgs, list[RuleSummary]]:
    async def handler(args: a.ListRulesArgs, _caller_scope: CallerScope) -> list[RuleSummary]:
        platform = args.platform.value if args.platform else None
        return await ports.rule.list_rules(
            args.business_id, platform=platform, enabled=args.enabled
        )

    return handler


def _get_rule(ports: ReadModelPorts) -> Handler[a.GetRuleArgs, RuleDetail]:
    async def handler(args: a.GetRuleArgs, _caller_scope: CallerScope) -> RuleDetail:
        return await ports.rule.get_rule(args.business_id, args.rule_id)

    return handler


def _explain_rule(ports: ReadModelPorts) -> Handler[a.ExplainRuleArgs, RuleExplanation]:
    async def handler(args: a.ExplainRuleArgs, _caller_scope: CallerScope) -> RuleExplanation:
        return await ports.rule.explain_rule(
            args.business_id, args.rule_id, entity_ref=args.entity_ref
        )

    return handler


def _list_guardrails(ports: ReadModelPorts) -> Handler[a.ListGuardrailsArgs, list[GuardrailInfo]]:
    async def handler(
        args: a.ListGuardrailsArgs, _caller_scope: CallerScope
    ) -> list[GuardrailInfo]:
        return await ports.rule.list_guardrails(args.business_id, args.scope_ref)

    return handler


def _get_kill_switch_status(
    ports: ReadModelPorts,
) -> Handler[a.GetKillSwitchStatusArgs, KillSwitchStatus]:
    async def handler(
        args: a.GetKillSwitchStatusArgs, _caller_scope: CallerScope
    ) -> KillSwitchStatus:
        return await ports.rule.get_kill_switch_status(args.business_id)

    return handler


# --- proposals -------------------------------------------------------


def _list_proposals(ports: ReadModelPorts) -> Handler[a.ListProposalsArgs, Page[ProposalSummary]]:
    async def handler(
        args: a.ListProposalsArgs, _caller_scope: CallerScope
    ) -> Page[ProposalSummary]:
        limit, cursor = page_params(args.page)
        state = args.state.value if args.state else None
        return await ports.proposal.list_proposals(
            args.business_id, state=state, cause_key=args.cause_key, limit=limit, cursor=cursor
        )

    return handler


def _get_proposal(ports: ReadModelPorts) -> Handler[a.GetProposalArgs, ProposalDetail]:
    async def handler(args: a.GetProposalArgs, _caller_scope: CallerScope) -> ProposalDetail:
        return await ports.proposal.get_proposal(args.business_id, args.proposal_id)

    return handler


# --- catalog ---------------------------------------------------------------


def _list_offerings(ports: ReadModelPorts) -> Handler[a.ListOfferingsArgs, list[OfferingSummary]]:
    async def handler(
        args: a.ListOfferingsArgs, _caller_scope: CallerScope
    ) -> list[OfferingSummary]:
        return await ports.catalog.list_offerings(args.business_id)

    return handler


def _list_calendar_events(
    ports: ReadModelPorts,
) -> Handler[a.ListCalendarEventsArgs, list[CalendarEventSummary]]:
    async def handler(
        args: a.ListCalendarEventsArgs, _caller_scope: CallerScope
    ) -> list[CalendarEventSummary]:
        summaries = await ports.catalog.list_calendar_events(
            args.business_id, open_only=args.open_only
        )
        if args.kind is None:
            return summaries
        return [summary for summary in summaries if summary.kind == args.kind]

    return handler


def _get_calendar_event(
    ports: ReadModelPorts,
) -> Handler[a.GetCalendarEventArgs, CalendarEventDetail]:
    async def handler(
        args: a.GetCalendarEventArgs, _caller_scope: CallerScope
    ) -> CalendarEventDetail:
        return await ports.catalog.get_calendar_event(args.business_id, args.calendar_event_id)

    return handler


# --- audit / decision log -------------------------------------------------


def _search_decision_log(
    ports: ReadModelPorts,
) -> Handler[a.SearchDecisionLogArgs, Page[DecisionLogEntrySummary]]:
    async def handler(
        args: a.SearchDecisionLogArgs, _caller_scope: CallerScope
    ) -> Page[DecisionLogEntrySummary]:
        limit, cursor = page_params(args.page)
        return await ports.audit.search_decision_log(
            args.business_id,
            since=args.since,
            until=args.until,
            event_type=args.event_type,
            entity_ref=args.entity_ref,
            limit=limit,
            cursor=cursor,
        )

    return handler


def _get_decision_log_entry(
    ports: ReadModelPorts,
) -> Handler[a.GetDecisionLogEntryArgs, DecisionLogEntryDetail]:
    async def handler(
        args: a.GetDecisionLogEntryArgs, _caller_scope: CallerScope
    ) -> DecisionLogEntryDetail:
        return await ports.audit.get_decision_log_entry(args.business_id, args.seq)

    return handler


# --- creative ---------------------------------------------------------


def _list_creative_briefs(
    ports: ReadModelPorts,
) -> Handler[a.ListCreativeBriefsArgs, list[CreativeBriefSummary]]:
    async def handler(
        args: a.ListCreativeBriefsArgs, _caller_scope: CallerScope
    ) -> list[CreativeBriefSummary]:
        return await ports.creative.list_creative_briefs(args.business_id)

    return handler


def _get_creative_job(ports: ReadModelPorts) -> Handler[a.GetCreativeJobArgs, CreativeJobStatus]:
    async def handler(args: a.GetCreativeJobArgs, _caller_scope: CallerScope) -> CreativeJobStatus:
        return await ports.creative.get_creative_job(args.business_id, args.job_id)

    return handler


# --- brand ----------------------------------------------------------------


def _get_brand_kit(ports: ReadModelPorts) -> Handler[a.GetBrandKitArgs, BrandKitDetail]:
    async def handler(args: a.GetBrandKitArgs, _caller_scope: CallerScope) -> BrandKitDetail:
        return await ports.brand.get_brand_kit(args.business_id)

    return handler


def _list_brand_assets(
    ports: ReadModelPorts,
) -> Handler[a.ListBrandAssetsArgs, list[BrandAssetSummary]]:
    async def handler(
        args: a.ListBrandAssetsArgs, _caller_scope: CallerScope
    ) -> list[BrandAssetSummary]:
        kind = args.kind.value if args.kind else None
        return await ports.brand.list_brand_assets(args.business_id, kind=kind)

    return handler


# --- contexto de proyecto / capacidades ------------------------------------


def _get_project_context(
    ports: ReadModelPorts, clock: Clock
) -> Handler[a.GetProjectContextArgs, dict[str, object]]:
    use_case = GetProjectContext(ports, clock)

    async def handler(
        args: a.GetProjectContextArgs, _caller_scope: CallerScope
    ) -> dict[str, object]:
        return await use_case.execute(args.business_id)

    return handler


def _get_capabilities(
    ports: ReadModelPorts, clock: Clock
) -> Handler[a.GetCapabilitiesArgs, CapabilitiesReport]:
    use_case = GetCapabilities(ports, clock)

    async def handler(
        args: a.GetCapabilitiesArgs, _caller_scope: CallerScope
    ) -> CapabilitiesReport:
        return await use_case.execute(args.business_id)

    return handler
