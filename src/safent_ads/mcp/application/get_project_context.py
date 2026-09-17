"""`GetProjectContext`: caso de uso detras de la herramienta MCP
`get_project_context` -- el paquete que hace cierta la frase del
propietario "hazme unos banners para una campana de Display de X" sin que
el agente tenga que encadenar quince llamadas primero. Ensamblado puro
sobre `ReadModelPorts` ya existentes (plan.md §4: leidos, nunca
importados): ningun almacen propio, ningun dato duplicado."""

from __future__ import annotations

from safent_ads.mcp.application.dto import (
    AutonomyStatus,
    BrandKitSummary,
    BusinessSummary,
    CalendarEventDetail,
    GuardrailInfo,
    InventorySummary,
    ProposalState,
    ProposalSummary,
    SignalSummary,
    TopMover,
    Window,
    WindowPreset,
)
from safent_ads.mcp.application.errors import EntityNotFoundError
from safent_ads.mcp.domain.capabilities import missing_platform_accounts, resolve_autonomy_status
from safent_ads.mcp.domain.context_pack import (
    cap_payload_size,
    summarize_brand_kit,
    summarize_inventory,
    top_and_bottom_movers,
)
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.shared.clock import Clock
from safent_ads.shared.read_models.serialization import to_json_value

_MAX_CALENDAR_EVENTS = 10
_MAX_OFFERINGS = 50
_MAX_CAMPAIGNS_FOR_INVENTORY = 200
_MAX_SIGNALS = 20
_MAX_PROPOSALS = 20
_MAX_TOP_MOVERS = 5
_PORTFOLIO_WINDOW = Window(
    preset=WindowPreset.THIRTY_DAYS, lag_days=0, date_from=None, date_to=None
)
_NO_BRAND_KIT_NOTE = "sin kit de marca configurado: cargar config/brand/<negocio>.yaml"


class GetProjectContext:
    def __init__(self, ports: ReadModelPorts, clock: Clock) -> None:
        self._ports = ports
        self._clock = clock

    async def execute(self, business_id: str) -> dict[str, object]:
        business = await self._resolve_business(business_id)
        platform_accounts = await self._ports.portfolio.list_platform_accounts(business_id)
        open_calendar_events = await self._open_calendar_events(business_id)
        inventory = await self._inventory(business_id)
        top_performers, bottom_performers = await self._performers(business_id)
        signals = await self._signals(business_id)
        open_proposals = await self._open_proposals(business_id)
        autonomy, guardrails = await self._autonomy_and_guardrails(business_id)
        brand_kit, capability_notes = await self._brand_kit(business_id)
        offerings = (await self._ports.catalog.list_offerings(business_id))[:_MAX_OFFERINGS]
        freshness = await self._ports.portfolio.get_data_freshness(business_id)
        capability_notes = capability_notes + missing_platform_accounts(platform_accounts)

        payload = to_json_value(
            {
                "business": business,
                "platform_accounts": platform_accounts,
                "open_calendar_events": open_calendar_events,
                "inventory": inventory,
                "top_performers": top_performers,
                "bottom_performers": bottom_performers,
                "signals": signals,
                "open_proposals": open_proposals,
                "autonomy": autonomy,
                "guardrails": guardrails,
                "brand_kit": brand_kit,
                "offerings": offerings,
                "freshness": freshness,
                "capability_notes": capability_notes,
                "generated_at": self._clock.now(),
            }
        )
        return cap_payload_size(payload)

    async def _resolve_business(self, business_id: str) -> BusinessSummary:
        businesses = await self._ports.business_directory.list_businesses(
            frozenset({business_id})
        )
        if not businesses:
            raise EntityNotFoundError(f"negocio no encontrado: {business_id}")
        return businesses[0]

    async def _open_calendar_events(self, business_id: str) -> list[CalendarEventDetail]:
        summaries = await self._ports.catalog.list_calendar_events(business_id, open_only=True)
        details: list[CalendarEventDetail] = []
        for summary in summaries[:_MAX_CALENDAR_EVENTS]:
            details.append(
                await self._ports.catalog.get_calendar_event(
                    business_id, summary.calendar_event_id
                )
            )
        return details

    async def _inventory(self, business_id: str) -> InventorySummary:
        page = await self._ports.entity.list_campaigns(
            business_id,
            platform=None,
            status=None,
            limit=_MAX_CAMPAIGNS_FOR_INVENTORY,
            cursor=None,
        )
        return summarize_inventory(page, requested_limit=_MAX_CAMPAIGNS_FOR_INVENTORY)

    async def _performers(self, business_id: str) -> tuple[list[TopMover], list[TopMover]]:
        overview = await self._ports.portfolio.get_portfolio_overview(
            business_id, _PORTFOLIO_WINDOW
        )
        return top_and_bottom_movers(overview.top_movers, limit=_MAX_TOP_MOVERS)

    async def _signals(self, business_id: str) -> list[SignalSummary]:
        page = await self._ports.signal.list_signals(
            business_id, kind=None, min_strength=None, since=None, limit=_MAX_SIGNALS, cursor=None
        )
        return page.items

    async def _open_proposals(self, business_id: str) -> list[ProposalSummary]:
        page = await self._ports.proposal.list_proposals(
            business_id,
            state=ProposalState.PENDING.value,
            cause_key=None,
            limit=_MAX_PROPOSALS,
            cursor=None,
        )
        return page.items

    async def _autonomy_and_guardrails(
        self, business_id: str
    ) -> tuple[AutonomyStatus, list[GuardrailInfo]]:
        kill_switch = await self._ports.rule.get_kill_switch_status(business_id)
        rules = await self._ports.rule.list_rules(business_id, platform=None, enabled=True)
        autonomy = resolve_autonomy_status(kill_switch, rules)
        guardrails = await self._ports.rule.list_guardrails(business_id, business_id)
        return autonomy, guardrails

    async def _brand_kit(self, business_id: str) -> tuple[BrandKitSummary | None, list[str]]:
        try:
            detail = await self._ports.brand.get_brand_kit(business_id)
        except EntityNotFoundError:
            return None, [_NO_BRAND_KIT_NOTE]
        return summarize_brand_kit(detail), []
