"""`build_default_registry` (T045): arma el `ToolRegistry` con las
herramientas de lectura de `contracts/mcp-tools.md` mas las 4 que anade
esta lane (`get_brand_kit`, `list_brand_assets`, `get_project_context`,
`get_capabilities`, tool-surface.md §2.2/§6) y, si se pasa un
`ProposalWritePort` (US2/US3), las escrituras (`propose_*`,
`withdraw_proposal`, `apply_defensive_action`). Unico punto que conoce a la
vez los modelos de `args.py` y los handlers de `handlers.py`/`write_handlers.py`.

`experiment_tools.py` (T201), `opportunity_tools.py` (T114,
`propose_campaign`/`list_opportunities`), `economics_tools.py`/
`optimization_tools.py` (las 9 de profitability-engine.md §8: B-1,
checklists/final-review.md), `creative_generation_tools.py`
(`generate_creative_assets`/`run_creative_policy_check`, mismo B-1) y
`connection_tools.py` (`connect_platform_account`/`get_connection_status`,
Anadido del dueno 15-sep) son modulos autonomos que se enganchan aqui con
una linea cada uno, sin tocar lo anterior.

`registries_by_permission` (004 tasks.md A5) deriva los tres registros por
permiso de `ToolDefinition.tool_class` -- nunca una lista de nombres escrita
a mano: `ver` = `READ`; `proponer`/`aprobar` = `READ`+`PROPOSAL`+
`CATALOG_WRITE`; `aprobar` ademas `CONNECTION_WRITE` (conectar una cuenta de
plataforma es una decision de empresa, exclusiva del permiso mas alto)."""

from __future__ import annotations

from typing import Any

from safent_ads.catalog.application.create_offering import CreateOffering
from safent_ads.economics.application.query_service import EconomicsQueryService
from safent_ads.mcp.application.caller_scope import Permission
from safent_ads.mcp.application.proposal_write_port import ProposalWritePort
from safent_ads.mcp.presentation import args as a
from safent_ads.mcp.presentation.campaign_draft_tools import build_draft_tools
from safent_ads.mcp.presentation.cloudflare_tools import (
    CloudflareToolServices,
    build_cloudflare_tool_definitions,
)
from safent_ads.mcp.presentation.company_tools import (
    CompanyToolServices,
    build_company_tool_definitions,
)
from safent_ads.mcp.presentation.competitor_tools import (
    CompetitorToolServices,
    build_competitor_tool_definitions,
)
from safent_ads.mcp.presentation.connection_tools import (
    ConnectionToolServices,
    build_connection_tool_definitions,
)
from safent_ads.mcp.presentation.creative_generation_tools import (
    CreativeGenerationToolServices,
    build_creative_generation_tool_definitions,
)
from safent_ads.mcp.presentation.creative_upload_tools import (
    CreativeUploadToolServices,
    build_creative_upload_tool_definitions,
)
from safent_ads.mcp.presentation.economics_tools import build_economics_tool_definitions
from safent_ads.mcp.presentation.experiment_tools import (
    ExperimentToolServices,
    build_experiment_tool_definitions,
)
from safent_ads.mcp.presentation.handlers import build_handlers
from safent_ads.mcp.presentation.kit_tools import KitToolServices, build_kit_tool_definitions
from safent_ads.mcp.presentation.native_ads_tools import (
    NativeAdsToolServices,
    build_native_ads_tool_definitions,
)
from safent_ads.mcp.presentation.native_write_tools import build_native_write_tool_definitions
from safent_ads.mcp.presentation.offering_tools import build_offering_tool
from safent_ads.mcp.presentation.opportunity_tools import (
    OpportunityToolServices,
    build_opportunity_tool_definitions,
)
from safent_ads.mcp.presentation.optimization_tools import build_optimization_tool_definitions
from safent_ads.mcp.presentation.optimization_write_tools import (
    build_optimization_write_tool_definitions,
)
from safent_ads.mcp.presentation.package_tools import (
    PackageToolServices,
    build_package_tool_definitions,
)
from safent_ads.mcp.presentation.passthrough_tools import (
    PassthroughToolServices,
    build_passthrough_tool_definitions,
)
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.mcp.presentation.reference_data_tools import (
    ReferenceDataToolServices,
    build_reference_data_tool_definitions,
)
from safent_ads.mcp.presentation.registry import ToolClass, ToolDefinition, ToolRegistry
from safent_ads.mcp.presentation.search_terms_tools import (
    SearchTermsToolServices,
    build_search_terms_tool_definitions,
)
from safent_ads.mcp.presentation.write_handlers import build_write_handlers
from safent_ads.opportunities.infrastructure.campaign_drafts_sql import CampaignDraftStore
from safent_ads.optimization.application.query_service import OptimizationQueryService
from safent_ads.proposals.domain.google_channel_spec import GoogleAdvertisingChannelType
from safent_ads.shared.clock import Clock

# `ver` -> READ; `proponer` -> +PROPOSAL+CATALOG_WRITE+CREATIVE_WRITE;
# `aprobar` -> ademas CONNECTION_WRITE. `aprobar` NO anade ninguna
# herramienta de decision: el registro rechaza `approve_*`/`execute_*` al
# construir (registry.py). `CREATIVE_WRITE` (004 tasks-2.md D-3, I1 punto
# 2) entra en `proponer`/`aprobar`, nunca en `ver` -- mismo patron que
# `CATALOG_WRITE`.
_CLASSES_BY_PERMISSION: dict[Permission, frozenset[ToolClass]] = {
    Permission.VIEW: frozenset({ToolClass.READ}),
    Permission.PROPOSE: frozenset(
        {
            ToolClass.READ,
            ToolClass.PROPOSAL,
            ToolClass.CATALOG_WRITE,
            ToolClass.CREATIVE_WRITE,
        }
    ),
    Permission.APPROVE: frozenset(
        {
            ToolClass.READ,
            ToolClass.PROPOSAL,
            ToolClass.CATALOG_WRITE,
            ToolClass.CREATIVE_WRITE,
            ToolClass.CONNECTION_WRITE,
        }
    ),
}


def registries_by_permission(registry: ToolRegistry) -> dict[Permission, ToolRegistry]:
    """Deriva un `ToolRegistry` por permiso del registro completo (`aprobar`),
    filtrando por `tool_class` -- nunca una lista de nombres mantenida a
    mano (contracts/mcp.md §3)."""
    return {
        permission: ToolRegistry(
            definition for definition in registry if definition.tool_class in classes
        )
        for permission, classes in _CLASSES_BY_PERMISSION.items()
    }


def _by_business_id(args: Any) -> str:  # noqa: ANN401 - extractor generico por posicion
    return str(args.business_id)


# Imagen generica por defecto (lane 006-cloudflare-ui): la descripcion de
# `list_businesses` nunca lleva un nombre de cliente fijo -- `build_default_
# registry(brand_name=...)` la rellena con `ApiSettings.brand_name`
# (`ADS_BRAND_NAME`, "tu negocio" por defecto), nunca el nombre de un
# cliente a pie de letra.
_DEFAULT_BRAND_NAME = "tu negocio"
_LIST_BUSINESSES_DESCRIPTION_TEMPLATE = (
    "Negocios de {brand_name} que tu puesto puede ver. Sin argumentos aparte de la "
    "credencial: el alcance lo fija el puesto, no un `business_id` que pidas tu."
)

_CATALOG: tuple[tuple[str, str, type[a.ToolArgs]], ...] = (
    (
        "list_businesses",
        _LIST_BUSINESSES_DESCRIPTION_TEMPLATE.format(brand_name=_DEFAULT_BRAND_NAME),
        a.ListBusinessesArgs,
    ),
    (
        "list_platform_accounts",
        "Cuentas de Meta y Google conectadas a un negocio, con su moneda, zona horaria "
        "y nivel de API. Usala antes de proponer nada para saber que cuentas existen.",
        a.ListPlatformAccountsArgs,
    ),
    (
        "get_portfolio_overview",
        "Resumen de gasto, ritmo, conversiones y coste por lead/venta de todo el "
        "negocio en una ventana de tiempo (p.ej. 7D). Punto de partida habitual.",
        a.GetPortfolioOverviewArgs,
    ),
    (
        "get_data_freshness",
        "Ultima vez que se sincronizaron los datos de cada cuenta, en minutos. Usala "
        "antes de decidir algo con metricas: datos viejos no deben guiar una propuesta.",
        a.GetDataFreshnessArgs,
    ),
    (
        "list_campaigns",
        "Campanas de un negocio, con estado (activa/pausada), presupuesto diario y "
        "plataforma. Filtra opcionalmente por `platform`/`status`; pagina con `cursor`.",
        a.ListCampaignsArgs,
    ),
    (
        "get_campaign",
        "Detalle de una campana concreta (`entity_ref`, p.ej. `google:campaign:123`): "
        "estado, presupuesto y si aprende automaticamente.",
        a.GetCampaignArgs,
    ),
    (
        "list_ad_sets",
        "Conjuntos de anuncios (ad sets/grupos) de una campana, con su estado y si "
        "estan en fase de aprendizaje.",
        a.ListAdSetsArgs,
    ),
    (
        "list_ads",
        "Anuncios dentro de un conjunto, con su estado. Usala antes de proponer una "
        "pausa o un cambio de presupuesto para confirmar el `entity_ref` correcto.",
        a.ListAdsArgs,
    ),
    (
        "list_creatives",
        "Creatividades (imagenes/videos) disponibles para un negocio, con su formato "
        "y estado de revision de politica.",
        a.ListCreativesArgs,
    ),
    (
        "get_creative",
        "Detalle de una creatividad concreta por `asset_id`: dimensiones, senal de "
        "origen si la generó una regla, y estado.",
        a.GetCreativeArgs,
    ),
    (
        "get_entity_metrics",
        "Serie temporal de gasto, conversiones y coste por conversion de una entidad "
        "(campana/conjunto/anuncio) en una ventana, con granularidad diaria u horaria.",
        a.GetEntityMetricsArgs,
    ),
    (
        "get_insights",
        "Metricas de plataforma cacheadas (impresiones, clics, CTR) de una entidad en "
        "una ventana, con desglose opcional (p.ej. por dispositivo).",
        a.GetInsightsArgs,
    ),
    (
        "run_gaql",
        "Consulta Google Ads Query Language de solo lectura contra una cuenta (nunca "
        "INSERT/UPDATE/DELETE: el propio validador los rechaza). Para preguntas que "
        "las demas herramientas no cubren.",
        a.RunGaqlArgs,
    ),
    (
        "list_signals",
        "Senales detectadas (subidas/bajadas de rendimiento) de un negocio, con su "
        "fuerza (0-100) y si ya se confirmaron. Filtra por `kind`/`min_strength`.",
        a.ListSignalsArgs,
    ),
    (
        "get_signal",
        "Detalle de una senal concreta por `signal_id`: metrica, causa y ventana de "
        "evaluacion.",
        a.GetSignalArgs,
    ),
    (
        "explain_signal",
        "Narrativa en castellano llano de por que se disparo una senal (p.ej. \"CPL 41 "
        "€ vs objetivo 28 €\"), lista para citar como `cause.text` en una propuesta.",
        a.ExplainSignalArgs,
    ),
    (
        "list_anomalies",
        "Anomalias estadisticas detectadas desde una fecha (`since`), con su "
        "puntuacion de desviacion. Distinto de `list_signals`: aqui no hay causa aun.",
        a.ListAnomaliesArgs,
    ),
    (
        "get_pacing",
        "Ritmo de gasto de una entidad frente a su presupuesto: indice de pacing y "
        "proyeccion de gasto a fin de mes.",
        a.GetPacingArgs,
    ),
    (
        "list_rules",
        "Reglas automaticas configuradas para un negocio (p.ej. M05 baja presupuesto "
        "si CPL sube), con si estan activas y su nivel de autonomia.",
        a.ListRulesArgs,
    ),
    (
        "get_rule",
        "Detalle de una regla por `rule_id`: condicion, ventana de evaluacion y accion "
        "que dispara.",
        a.GetRuleArgs,
    ),
    (
        "explain_rule",
        "Simula en seco si una regla dispararia ahora mismo para una entidad, sin "
        "ejecutar nada: util antes de proponer un cambio manual que la regla ya cubre.",
        a.ExplainRuleArgs,
    ),
    (
        "list_guardrails",
        "Guardarrailes efectivos (topes de gasto, limites de cambio) para un ambito "
        "(cuenta/negocio): el techo real contra el que se evalua cualquier propuesta.",
        a.ListGuardrailsArgs,
    ),
    (
        "get_kill_switch_status",
        "Si el freno de emergencia esta activo para un negocio, y desde cuando. Con el "
        "freno activo ninguna accion automatica se ejecuta, aunque este aprobada.",
        a.GetKillSwitchStatusArgs,
    ),
    (
        "list_proposals",
        "Propuestas pendientes o resueltas de un negocio, filtrables por estado "
        "(`state`) o causa (`cause_key`). Usala para ver que sigue esperando al dueno.",
        a.ListProposalsArgs,
    ),
    (
        "get_proposal",
        "Detalle completo de una propuesta por `proposal_id`: diff, causa, evidencia e "
        "impacto estimado en euros.",
        a.GetProposalArgs,
    ),
    (
        "list_offerings",
        "Catalogo de productos u ofertas vendibles del negocio (p.ej. \"Citas "
        "veterinarias\"), con precio. Necesario antes de `propose_campaign`.",
        a.ListOfferingsArgs,
    ),
    (
        "list_calendar_events",
        "Hitos de calendario del negocio (lanzamientos, temporadas) que pueden "
        "justificar una campana o un cambio de ritmo. `open_only` filtra los vigentes.",
        a.ListCalendarEventsArgs,
    ),
    (
        "get_calendar_event",
        "Detalle de un hito de calendario por `calendar_event_id`: fechas de inicio y "
        "fin, y la oferta a la que esta ligado.",
        a.GetCalendarEventArgs,
    ),
    (
        "search_decision_log",
        "Bitacora de decisiones del negocio (que se hizo, cuando y por quien), "
        "filtrable por tipo de evento, entidad y ventana de fechas. Cadena verificable.",
        a.SearchDecisionLogArgs,
    ),
    (
        "get_decision_log_entry",
        "Una entrada concreta de la bitacora por `seq`, con su payload completo.",
        a.GetDecisionLogEntryArgs,
    ),
    (
        "list_creative_briefs",
        "Briefs de creatividad (encargos de generacion de imagen/texto) de un negocio, "
        "con su estado (borrador/listo).",
        a.ListCreativeBriefsArgs,
    ),
    (
        "get_creative_job",
        "Estado de un trabajo de generacion de creatividad por `job_id`: progreso "
        "(0-1) y errores si los hay. La generacion real la dispara `generate_creative_assets`.",
        a.GetCreativeJobArgs,
    ),
    (
        "get_brand_kit",
        "Kit de marca completo de un negocio: tipografia, paleta de color, tono de voz "
        "y textos prohibidos/permitidos (`claims_allowlist`/`forbidden_claims`).",
        a.GetBrandKitArgs,
    ),
    (
        "list_brand_assets",
        "Logos y fotos de referencia ya aprobados del negocio, filtrables por `kind` "
        "(p.ej. `logo_vector`). Usalos como referencia antes de generar creatividad nueva.",
        a.ListBrandAssetsArgs,
    ),
    (
        "get_project_context",
        "Paquete de contexto del negocio en una llamada: cartera, cuentas, "
        "calendario, inventario, senales, propuestas, marca y guardarrailes.",
        a.GetProjectContextArgs,
    ),
    (
        "get_capabilities",
        "Capacidades y configuración. platform_writes se refiere solo a escritura directa, "
        "no a preparar propuestas mediante propose_*. autonomy se refiere solo a reglas AUTO, "
        "no a analizar métricas ni proponer campañas. Las claves de generación ausentes son "
        "opcionales: no bloquean textos ni borradores con datos pendientes. Este informe no "
        "certifica entrega de anuncios ni un agente 24/7 en ejecución.",
        a.GetCapabilitiesArgs,
    ),
)

# contracts/mcp-tools.md §Escrituras + §apply_defensive_action. Todas
# `PROPOSAL`: crean/retiran una `PropuestaDeAccion` pendiente, o (solo
# `apply_defensive_action`) pasan por el chokepoint con una regla `AUTO` ya
# autorizada por el servicio -- ningun verbo de decision (`is_proposal_verb`
# ya las distingue de `approve_*`/`execute_*`, prohibidos por
# `ToolRegistry`). `propose_campaign` vive aparte, en
# `opportunity_tools.py` (T114): crear una campana nueva no tenia un
# `EntityRef` que la represente antes de existir en la plataforma --
# resuelto por `0027_us5_opportunities` (EntityLevel.ACCOUNT).
_WRITE_CATALOG: tuple[tuple[str, str, type[a.ToolArgs]], ...] = (
    (
        "propose_budget_change",
        "Propone subir o bajar el presupuesto diario de una campana/conjunto (en la "
        "divisa de la cuenta). Crea una propuesta pendiente; el dueno aprueba antes "
        "de que cambie nada en la plataforma.",
        a.ProposeBudgetChangeArgs,
    ),
    (
        "propose_pause",
        "Propone pausar una entidad activa (campana/conjunto/anuncio). Crea una "
        "propuesta pendiente; no pausa nada hasta que el dueno la aprueba en el panel.",
        a.ProposePauseArgs,
    ),
    (
        "propose_ad_child",
        "Propone un grupo/conjunto o anuncio PAUSED mediante child_plan explícito v1. "
        "Google SEARCH/manual CPC/RSA; Meta Traffic CBO/LINK_CLICKS. El anuncio Meta admite "
        "creative_id de la misma cuenta o creative_inline explícito: Página autorizada, "
        "imagen HTTPS pública, landing, textos y CTA, en una sola creación PAUSED. "
        "No activa, no sube archivos ni aprueba. Espera confirmación e inventario del padre "
        "antes de proponer el siguiente nivel; nunca inventes IDs.",
        a.ProposeAdChildArgs,
    ),
    (
        "propose_targeting_change",
        "Propone un cambio de segmentacion (audiencia, ubicaciones, palabras clave) "
        "para una entidad. Crea una propuesta pendiente de aprobacion del dueno.",
        a.ProposeTargetingChangeArgs,
    ),
    (
        "propose_creative_publication",
        "Propone publicar una o mas creatividades ya aprobadas en un conjunto de "
        "anuncios existente. No sube archivos nuevos: usa `asset_id`s ya existentes.",
        a.ProposeCreativePublicationArgs,
    ),
    (
        "withdraw_proposal",
        "Retira una propuesta propia todavia pendiente, antes de que el dueno la "
        "apruebe o rechace. No afecta a propuestas ya decididas.",
        a.WithdrawProposalArgs,
    ),
    (
        "apply_defensive_action",
        "Unica via del agente hacia el chokepoint: solo reglas AUTO ya disparando.",
        a.ApplyDefensiveActionArgs,
    ),
)


def build_default_registry(
    ports: ReadModelPorts,
    clock: Clock,
    write_port: ProposalWritePort | None = None,
    *,
    experiment_services: ExperimentToolServices | None = None,
    search_terms_services: SearchTermsToolServices | None = None,
    opportunity_services: OpportunityToolServices | None = None,
    economics_service: EconomicsQueryService | None = None,
    optimization_service: OptimizationQueryService | None = None,
    creative_generation_services: CreativeGenerationToolServices | None = None,
    native_ads_services: NativeAdsToolServices | None = None,
    offering_creation: CreateOffering | None = None,
    campaign_drafts: CampaignDraftStore | None = None,
    connection_services: ConnectionToolServices | None = None,
    reference_data_services: ReferenceDataToolServices | None = None,
    passthrough_services: PassthroughToolServices | None = None,
    competitor_services: CompetitorToolServices | None = None,
    company_services: CompanyToolServices | None = None,
    creative_upload_services: CreativeUploadToolServices | None = None,
    package_services: PackageToolServices | None = None,
    kit_services: KitToolServices | None = None,
    cloudflare_services: CloudflareToolServices | None = None,
    enabled_google_channels: frozenset[GoogleAdvertisingChannelType] = frozenset(
        {GoogleAdvertisingChannelType.SEARCH}
    ),
    brand_name: str = _DEFAULT_BRAND_NAME,
) -> ToolRegistry:
    handlers = build_handlers(ports, clock)
    definitions: list[ToolDefinition[Any]] = [
        ToolDefinition(
            name=name,
            description=(
                _LIST_BUSINESSES_DESCRIPTION_TEMPLATE.format(brand_name=brand_name)
                if name == "list_businesses"
                else description
            ),
            args_model=args_model,
            tool_class=ToolClass.READ,
            handler=handlers[name],
            business_id_of=None if name == "list_businesses" else _by_business_id,
        )
        for name, description, args_model in _CATALOG
    ]
    if campaign_drafts is not None:
        definitions.extend(
            build_draft_tools(
                campaign_drafts,
                accounts=ports.portfolio,
                enabled_google_channels=enabled_google_channels,
            )
        )
    if write_port is not None:
        write_handlers = build_write_handlers(write_port)
        definitions.extend(
            ToolDefinition(
                name=name,
                description=description,
                args_model=args_model,
                tool_class=ToolClass.PROPOSAL,
                handler=write_handlers[name],
                business_id_of=_by_business_id,
            )
            for name, description, args_model in _WRITE_CATALOG
        )
        # 004 tasks-2.md W2/W3: las 5 propuestas de optimizacion que
        # faltaban (`propose_resume`/`propose_bid_target`/
        # `propose_negative_keywords`/`propose_creative_rotation`/
        # `propose_delete`) + `propose_native_write` -- mismo `write_port`,
        # sin un segundo puerto ni un segundo guard de `None`.
        definitions.extend(build_optimization_write_tool_definitions(write_port))
        definitions.extend(build_native_write_tool_definitions(write_port))
    _extend_with_optional_tool_modules(
        definitions,
        experiment_services=experiment_services,
        search_terms_services=search_terms_services,
        opportunity_services=opportunity_services,
        economics_service=economics_service,
        optimization_service=optimization_service,
        creative_generation_services=creative_generation_services,
        native_ads_services=native_ads_services,
        offering_creation=offering_creation,
        connection_services=connection_services,
        reference_data_services=reference_data_services,
        passthrough_services=passthrough_services,
        competitor_services=competitor_services,
        company_services=company_services,
        creative_upload_services=creative_upload_services,
        package_services=package_services,
        kit_services=kit_services,
    )
    # integrations/cloudflare (Anadido del dueno, 14-sep; lane
    # 006-cloudflare-ui, 15-sep): 5 herramientas autonomas
    # (get_cloudflare_connection_status/list_dns_zones/list_dns_records/
    # upsert_dns_record/delete_dns_record), enganchadas aqui en vez de en
    # `_extend_with_optional_tool_modules` para no anadir un septimo
    # parametro a esa cadena -- una linea, mismo criterio de aislamiento.
    if cloudflare_services is not None:
        definitions.extend(build_cloudflare_tool_definitions(cloudflare_services))
    return ToolRegistry(definitions)


def _extend_with_optional_tool_modules(
    definitions: list[ToolDefinition[Any]],
    *,
    experiment_services: ExperimentToolServices | None,
    search_terms_services: SearchTermsToolServices | None,
    opportunity_services: OpportunityToolServices | None,
    economics_service: EconomicsQueryService | None,
    optimization_service: OptimizationQueryService | None,
    creative_generation_services: CreativeGenerationToolServices | None,
    native_ads_services: NativeAdsToolServices | None,
    offering_creation: CreateOffering | None,
    connection_services: ConnectionToolServices | None,
    reference_data_services: ReferenceDataToolServices | None,
    passthrough_services: PassthroughToolServices | None,
    competitor_services: CompetitorToolServices | None,
    company_services: CompanyToolServices | None,
    creative_upload_services: CreativeUploadToolServices | None,
    package_services: PackageToolServices | None,
    kit_services: KitToolServices | None,
) -> None:
    """Cada modulo de herramientas autonomo se engancha con una linea,
    igual que documenta el docstring del fichero -- separado de
    `build_default_registry` (y partido en dos mitades) solo para mantener
    la complejidad ciclomatica baja (Clean Code), nunca para cambiar el
    orden de registro."""
    _extend_with_pre_004_optional_modules(
        definitions,
        experiment_services=experiment_services,
        search_terms_services=search_terms_services,
        opportunity_services=opportunity_services,
        economics_service=economics_service,
        optimization_service=optimization_service,
        creative_generation_services=creative_generation_services,
        native_ads_services=native_ads_services,
        offering_creation=offering_creation,
    )
    _extend_with_004_optional_modules(
        definitions,
        connection_services=connection_services,
        reference_data_services=reference_data_services,
        passthrough_services=passthrough_services,
        competitor_services=competitor_services,
        company_services=company_services,
        creative_upload_services=creative_upload_services,
    )
    # 003-paquete-de-campana (T040): propose_campaign_package, autonoma del
    # resto del catalogo -- clase PROPOSAL, nunca en `ver`.
    if package_services is not None:
        definitions.extend(build_package_tool_definitions(package_services))
    # Kit de marketing del negocio (encargo del dueno, 14-sep):
    # list_kit_files/get_kit_text/get_kit_file, autonoma del resto del
    # catalogo -- 3 READ. `composition/app.py` siempre pasa `kit_services`
    # (nunca None): `ADS_KIT_DIR` ausente deja `KitToolServices.store=None`,
    # las tres herramientas siguen registradas y reportan `KIT_NOT_CONFIGURED`.
    if kit_services is not None:
        definitions.extend(build_kit_tool_definitions(kit_services))


def _extend_with_pre_004_optional_modules(
    definitions: list[ToolDefinition[Any]],
    *,
    experiment_services: ExperimentToolServices | None,
    search_terms_services: SearchTermsToolServices | None,
    opportunity_services: OpportunityToolServices | None,
    economics_service: EconomicsQueryService | None,
    optimization_service: OptimizationQueryService | None,
    creative_generation_services: CreativeGenerationToolServices | None,
    native_ads_services: NativeAdsToolServices | None,
    offering_creation: CreateOffering | None,
) -> None:
    # T201 (profitability-engine.md §4/§6/§8): design_experiment/propose_experiment/
    # get_experiment_status/get_calibration_report, autonomas del resto del catalogo.
    if experiment_services is not None:
        definitions.extend(build_experiment_tool_definitions(experiment_services))
    # tool-surface.md §2.1 P2: list_search_terms/get_budget_envelope, autonomas del resto.
    if search_terms_services is not None:
        definitions.extend(build_search_terms_tool_definitions(search_terms_services))
    # T114 (mcp/presentation/catalog.py:80-82): propose_campaign/list_opportunities.
    if opportunity_services is not None:
        definitions.extend(build_opportunity_tool_definitions(opportunity_services))
    # B-1 (checklists/final-review.md): las 5 de economia unitaria + las 4
    # de optimizacion restantes (profitability-engine.md §8), declaradas y
    # probadas desde antes pero nunca importadas por `composition/`.
    if economics_service is not None:
        definitions.extend(build_economics_tool_definitions(economics_service))
    if optimization_service is not None:
        definitions.extend(build_optimization_tool_definitions(optimization_service))
    # B-1: generate_creative_assets/run_creative_policy_check, mismo hueco.
    if creative_generation_services is not None:
        definitions.extend(build_creative_generation_tool_definitions(creative_generation_services))
    if native_ads_services is not None:
        definitions.extend(build_native_ads_tool_definitions(native_ads_services))
    if offering_creation is not None:
        definitions.append(build_offering_tool(offering_creation))


def _extend_with_004_optional_modules(
    definitions: list[ToolDefinition[Any]],
    *,
    connection_services: ConnectionToolServices | None,
    reference_data_services: ReferenceDataToolServices | None,
    passthrough_services: PassthroughToolServices | None,
    competitor_services: CompetitorToolServices | None,
    company_services: CompanyToolServices | None,
    creative_upload_services: CreativeUploadToolServices | None,
) -> None:
    # Anadido del dueno (15-sep): connect_platform_account/get_connection_status,
    # unicas CONNECTION_WRITE -- solo `aprobar` las ve.
    if connection_services is not None:
        definitions.extend(build_connection_tool_definitions(connection_services))
    # 004 tasks-2.md R3/R4 (historia 18): datos de referencia de Meta y
    # Google, autonomas del resto del catalogo.
    if reference_data_services is not None:
        definitions.extend(build_reference_data_tool_definitions(reference_data_services))
    # R5 (historia 19): get_meta_graph, paso a traves de lectura de Meta.
    if passthrough_services is not None:
        definitions.extend(build_passthrough_tool_definitions(passthrough_services))
    # R7 (historias 21-23): get_competitor_links/search_competitor_ads.
    if competitor_services is not None:
        definitions.extend(build_competitor_tool_definitions(competitor_services))
    # R2/R8 (historias 12/24): get_crm_summary/list_top_performing_ads.
    if company_services is not None:
        definitions.extend(build_company_tool_definitions(company_services))
    # W4 (historia 13): upload_creative_asset -- unica `CREATIVE_WRITE`
    # (D-3), visible en `proponer`/`aprobar`, nunca en `ver`.
    if creative_upload_services is not None:
        definitions.extend(
            build_creative_upload_tool_definitions(
                creative_upload_services, tool_class=ToolClass.CREATIVE_WRITE
            )
        )
