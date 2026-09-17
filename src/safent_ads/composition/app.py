"""Fabrica de la app FastAPI de `ads-api`: monta `/api/v1/health` (liveness,
sin autenticacion, contracts/rest-api.md) y el servidor MCP en `/mcp`
(contracts/mcp-tools.md). Routers de negocio llegan por fase (plan.md §10);
F0 solo cablea el esqueleto.

El `lifespan` de la app padre entra explicitamente en el `lifespan_context`
de la sub-app MCP: Starlette no reenvia el evento ASGI `lifespan` a las
sub-apps, asi que sin esto el `StreamableHTTPSessionManager` del SDK nunca
arranca su task group y toda peticion autenticada revienta con
`RuntimeError: Task group is not initialized`."""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import structlog
import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Match, Route
from starlette.types import Lifespan, Scope

from safent_ads import __version__
from safent_ads.accounts.infrastructure.native_ads_broker_client import NativeAdsBrokerClient
from safent_ads.accounts.infrastructure.oauth_broker_client import OAuthBrokerSocketClient
from safent_ads.accounts.presentation.composio_internal_router import build_composio_internal_router
from safent_ads.audit.application.record_decision import RecordDecision
from safent_ads.audit.domain.entry import ActorKind, DecisionKind, PendingDecision
from safent_ads.audit.infrastructure.sql_repository import SqlDecisionLogRepository
from safent_ads.brand.application.confirm_brand_draft import ConfirmBrandDraft
from safent_ads.brand.application.get_brand_asset_preview import GetBrandAssetPreview
from safent_ads.brand.application.get_brand_draft import GetBrandDraft
from safent_ads.brand.application.get_brand_kit import GetBrandKit
from safent_ads.brand.application.ingest_brand_from_website import IngestBrandFromWebsite
from safent_ads.brand.application.list_brand_assets import ListBrandAssets
from safent_ads.brand.application.update_brand_claims import UpdateBrandClaims
from safent_ads.brand.application.upload_brand_asset import UploadBrandAsset
from safent_ads.brand.infrastructure.local_brand_asset_storage import LocalBrandAssetStorage
from safent_ads.brand.infrastructure.sql_brand_claims_decision_recorder import (
    SqlBrandClaimsDecisionRecorder,
)
from safent_ads.brand.infrastructure.sql_brand_discovery_draft_repository import (
    RequestScopedBrandDiscoveryDraftRepository,
)
from safent_ads.brand.infrastructure.sql_brand_kit_repository import (
    RequestScopedBrandKitRepository,
)
from safent_ads.brand.infrastructure.website_brand_extractor import WebsiteBrandExtractor
from safent_ads.brand.presentation.router import build_brand_router
from safent_ads.catalog.application.create_offering import CreateOffering
from safent_ads.catalog.infrastructure.sql_offering_creation import RequestScopedOfferingCreation
from safent_ads.catalog.presentation.rest import build_catalog_router
from safent_ads.composition.container import Container
from safent_ads.composition.creative_proposal_gateway import ContainerCreativeProposalGateway
from safent_ads.composition.creative_upload_adapter import ContainerCreativeUploadAdapter
from safent_ads.composition.credential_health_metrics import (
    run_credential_health_refresh_forever,
)
from safent_ads.composition.economics_rest import (
    build_economics_query_service,
    build_economics_read_router,
)
from safent_ads.composition.execution_rest import build_execution_router
from safent_ads.composition.execution_undo_adapter import ContainerSingleExecutionUndoAdapter
from safent_ads.composition.gaql_templates import load_gaql_template
from safent_ads.composition.managed_app import create_managed_app
from safent_ads.composition.mcp_write_adapter import ContainerProposalWriteAdapter
from safent_ads.composition.settings import ApiSettings, InstanceIdentity
from safent_ads.creative.application.generate_creative_assets import GenerateCreativeAssets
from safent_ads.creative.application.import_creative_asset import ImportCreativeAsset
from safent_ads.creative.application.ports import ImageRendererPort
from safent_ads.creative.application.propose_creative import ProposeCreative
from safent_ads.creative.application.run_policy_check import RunPolicyCheck
from safent_ads.creative.domain.enums import RendererName
from safent_ads.creative.domain.renderer_selector import RendererSelector
from safent_ads.creative.infrastructure.http_asset_fetcher import HttpAssetFetcher
from safent_ads.creative.infrastructure.in_process_gpu_queue import InProcessGpuQueue
from safent_ads.creative.infrastructure.local_asset_storage import LocalAssetStorage
from safent_ads.creative.infrastructure.policy_checker import LocalPolicyChecker
from safent_ads.creative.infrastructure.sql_repositories import (
    RequestScopedCreativeAssetRepository,
    RequestScopedCreativeBriefRepository,
    RequestScopedCreativeJobRepository,
)
from safent_ads.creative.presentation.router import build_creative_router
from safent_ads.crm.infrastructure.identity_salt import HkdfIdentitySalt
from safent_ads.crm.presentation.bridge_rest import build_crm_bridge_router
from safent_ads.crm.presentation.rest import build_conversions_router
from safent_ads.economics.presentation.customer_value_rest import build_customer_value_router
from safent_ads.economics.presentation.offerings_rest import build_offerings_router
from safent_ads.execution.infrastructure.sql_execution_read_port import (
    RequestScopedExecutionReadPort,
)
from safent_ads.execution.presentation.rest import build_execution_read_router
from safent_ads.iam.infrastructure.enterprise_seat_authority import (
    EnterpriseSeatAuthority,
)
from safent_ads.integrations.cloudflare import (
    DynamicCloudflareService,
    GetCloudflareConnectionStatus,
    build_cloudflare_connection_router,
    build_request_scoped_cloudflare_connection_store,
)
from safent_ads.logging_setup import configure_logging
from safent_ads.mcp.application.caller_scope import CallerScopeResolverPort, Permission
from safent_ads.mcp.application.creative_upload_port import CreativeUploadPort
from safent_ads.mcp.application.health import GetHealthStatus
from safent_ads.mcp.infrastructure.broker_competitor_research_port import (
    BrokerCompetitorResearchPort,
    MetaAdLibraryBrokerClient,
)
from safent_ads.mcp.infrastructure.broker_graph_passthrough_port import (
    BrokerGraphPassthroughPort,
    GraphPassthroughBrokerClient,
)
from safent_ads.mcp.infrastructure.broker_image_renderer_port import (
    BrokerImageRenderer,
    ImageRenderBrokerClient,
)
from safent_ads.mcp.infrastructure.broker_native_ads_read_port import BrokerNativeAdsReadPort
from safent_ads.mcp.infrastructure.broker_reference_data_port import (
    BrokerReferenceDataPort,
    GoogleKeywordIdeaBrokerClient,
    MetaReferenceDataBrokerClient,
)

# --- lane: surface ---
from safent_ads.mcp.infrastructure.broker_search_term_read_port import BrokerSearchTermReadPort
from safent_ads.mcp.infrastructure.enterprise_seat_caller_scope_resolver import (
    EnterpriseSeatCallerScopeResolver,
)
from safent_ads.mcp.infrastructure.env_capability_probe import EnvironmentCapabilityProbe
from safent_ads.mcp.infrastructure.local_kit_store import LocalKitStore
from safent_ads.mcp.infrastructure.rate_limiter import InMemoryQuota
from safent_ads.mcp.infrastructure.single_owner_caller_scope_resolver import (
    SingleOwnerCallerScopeResolver,
)
from safent_ads.mcp.infrastructure.sql_active_business_ids import SqlActiveBusinessIds
from safent_ads.mcp.infrastructure.sql_brand_read_port import SqlBrandReadPort
from safent_ads.mcp.infrastructure.sql_budget_envelope_read_port import (
    SqlBudgetEnvelopeReadPort,
)
from safent_ads.mcp.infrastructure.sql_business_directory import (
    SqlBusinessDirectory as SqlMcpBusinessDirectory,
)
from safent_ads.mcp.infrastructure.sql_creative_read_port import SqlCreativeReadPort
from safent_ads.mcp.infrastructure.sql_crm_read_port import SqlCrmSummaryReadPort
from safent_ads.mcp.infrastructure.sql_health_ports import (
    SqlAccountLinkStatusPort,
    SqlDatabaseHealthPort,
)
from safent_ads.mcp.infrastructure.sql_top_ads_read_port import SqlTopPerformingAdsReadPort
from safent_ads.mcp.presentation.catalog import build_default_registry, registries_by_permission
from safent_ads.mcp.presentation.cloudflare_tools import CloudflareToolServices
from safent_ads.mcp.presentation.company_tools import CompanyToolServices
from safent_ads.mcp.presentation.competitor_tools import CompetitorToolServices
from safent_ads.mcp.presentation.connection_tools import ConnectionToolServices
from safent_ads.mcp.presentation.creative_generation_tools import CreativeGenerationToolServices
from safent_ads.mcp.presentation.creative_upload_tools import CreativeUploadToolServices
from safent_ads.mcp.presentation.dispatcher import DecisionAuditPort, ToolDispatcher
from safent_ads.mcp.presentation.experiment_tools import ExperimentToolServices
from safent_ads.mcp.presentation.health import build_mcp_health_router
from safent_ads.mcp.presentation.http import (
    MCP_ENDPOINT_PATH,
    ContentLengthLimitMiddleware,
    SeatCredentialRouter,
    build_mcp_asgi_apps,
    build_mcp_instructions,
    build_mcp_servers,
)
from safent_ads.mcp.presentation.kit_tools import KitToolServices, build_kit_preview_router
from safent_ads.mcp.presentation.native_ads_tools import NativeAdsToolServices
from safent_ads.mcp.presentation.opportunity_tools import OpportunityToolServices
from safent_ads.mcp.presentation.package_tools import PackageToolServices
from safent_ads.mcp.presentation.passthrough_tools import PassthroughToolServices
from safent_ads.mcp.presentation.read_model_ports import ReadModelPorts
from safent_ads.mcp.presentation.reference_data_tools import ReferenceDataToolServices
from safent_ads.mcp.presentation.registry import ToolRegistry
from safent_ads.mcp.presentation.search_terms_tools import SearchTermsToolServices
from safent_ads.mcp_oauth.domain.scope import Scope as OAuthScope
from safent_ads.mcp_oauth.infrastructure.audit_redirect_uris import audit_client_redirect_uris
from safent_ads.mcp_oauth.infrastructure.prune_stale_clients import (
    run_prune_stale_oauth_state_forever,
)
from safent_ads.mcp_oauth.infrastructure.secrets_token_factory import SecretsOpaqueTokenFactory
from safent_ads.mcp_oauth.infrastructure.sha256_token_hasher import Sha256TokenHasher
from safent_ads.mcp_oauth.infrastructure.sql_oauth_session import SqlOAuthSession
from safent_ads.mcp_oauth.presentation.caller_scope import OAuthCallerScopeResolver
from safent_ads.mcp_oauth.presentation.consent_router import build_consent_router
from safent_ads.mcp_oauth.presentation.grants_router import build_grants_router
from safent_ads.mcp_oauth.presentation.routes import build_oauth_routes, well_known_not_found_route
from safent_ads.mcp_oauth.presentation.routes import issuer_url as oauth_issuer_url
from safent_ads.mcp_oauth.presentation.sdk_provider import SdkOAuthProvider
from safent_ads.mcp_oauth.presentation.token_verifier import CompositeTokenVerifier
from safent_ads.notifications.presentation.rest import build_telegram_pairing_router
from safent_ads.observability.server import start_metrics_server
from safent_ads.opportunities.application.list_opportunities import ListOpportunities
from safent_ads.opportunities.application.propose_campaign import ProposeCampaign
from safent_ads.opportunities.infrastructure.campaign_drafts_sql import CampaignDraftStore
from safent_ads.opportunities.infrastructure.request_scoped_repositories import (
    RequestScopedAccountDailyCap,
    RequestScopedActiveAccountLookup,
    RequestScopedCampaignProposals,
    RequestScopedOfferingExists,
    RequestScopedOpenOpportunities,
)
from safent_ads.opportunities.presentation.campaign_drafts_rest import build_campaign_drafts_router
from safent_ads.optimization.application.design_experiment import DesignExperiment
from safent_ads.optimization.application.get_calibration_report import GetCalibrationReport
from safent_ads.optimization.application.get_experiment_status import GetExperimentStatus
from safent_ads.optimization.application.propose_experiment import ProposeExperiment
from safent_ads.optimization.application.query_service import OptimizationQueryService
from safent_ads.optimization.infrastructure.request_scoped_repositories import (
    RequestScopedCalibrationInputs,
    RequestScopedContributionMargins,
    RequestScopedDiagnosisMetrics,
    RequestScopedExperimentProposals,
    RequestScopedExperimentRepository,
    RequestScopedMarginalEstimates,
    RequestScopedReallocationCandidates,
    RequestScopedReallocationProposals,
    RequestScopedResponseCurves,
)
from safent_ads.optimization.presentation.rest import build_optimization_router_over_sql
from safent_ads.packages.application.ports import PackageBudgetEnvelope
from safent_ads.packages.application.propose_campaign_package import ProposeCampaignPackage
from safent_ads.packages.infrastructure.creative_asset_lookup import PackageCreativeAssetLookup
from safent_ads.packages.infrastructure.null_publish_as_lookup import NullPublishAsLookup
from safent_ads.packages.infrastructure.request_scoped_repositories import (
    RequestScopedAccountDailyCap as RequestScopedPackageAccountDailyCap,
)
from safent_ads.packages.infrastructure.request_scoped_repositories import (
    RequestScopedActiveAccountLookup as RequestScopedPackageActiveAccountLookup,
)
from safent_ads.packages.infrastructure.request_scoped_repositories import (
    RequestScopedCampaignPackages,
    RequestScopedLandingDomainPolicy,
)
from safent_ads.packages.infrastructure.request_scoped_repositories import (
    RequestScopedOfferingExists as RequestScopedPackageOfferingExists,
)
from safent_ads.packages.presentation.panel_read import build_package_read_router
from safent_ads.packages.presentation.rest import build_package_admin_router
from safent_ads.panel.infrastructure.sql_cockpit_read_model import RequestScopedCockpitReadModel
from safent_ads.panel.infrastructure.sql_read_model import RequestScopedPanelReadPort
from safent_ads.panel.presentation.cockpit_rest import build_cockpit_router
from safent_ads.panel.presentation.rest import build_panel_router
from safent_ads.proposals.presentation.rest import build_proposal_admin_router
from safent_ads.rules.presentation.rest import build_rules_router
from safent_ads.settings.application.get_settings import GetSettings
from safent_ads.settings.application.update_settings import UpdateSettings
from safent_ads.settings.domain.value_objects import ActiveHours, DigestHour
from safent_ads.settings.infrastructure.sql_settings_repository import (
    RequestScopedSettingsRepository,
)
from safent_ads.settings.presentation.rest import build_settings_router
from safent_ads.shared.clock import Clock
from safent_ads.shared.crypto.hkdf import derive_key
from safent_ads.shared.ids import BusinessId

logger = structlog.get_logger(__name__)

# `LocalAssetStorage.signing_key` (previews de creative, security-review-f4.md
# B-1): derivada de `settings.session_secret` con HKDF-SHA256, nunca un
# secreto propio con `default=` publico. `info` fijo y distinto por uso para
# que esta subclave nunca coincida con otra derivada del mismo maestro.
_CREATIVE_PREVIEW_SIGNING_KEY_INFO = b"safent-ads/creative-preview/v1"


# --- lane: surface ---
def _build_connection_tool_services(container: Container) -> ConnectionToolServices:
    """`connect_platform_account`/`get_connection_status` (Anadido del
    dueno, 15-sep): mismo `OAuthBrokerSocketClient` que el router REST de
    conexiones ya usa (`accounts/presentation/connections_router.py`)."""
    return ConnectionToolServices(
        session_factory=container.session_factory,
        oauth_broker=OAuthBrokerSocketClient(container.settings.broker_socket_path),
        id_generator=container.id_generator,
        public_base_url=container.settings.public_base_url,
    )


def _build_creative_asset_store(container: Container, settings: ApiSettings) -> LocalAssetStorage:
    """Unica factoria de `LocalAssetStorage` (disco local): `_build_creative_router`
    (REST) y `_build_creative_upload_tool_services` (MCP, 004 tasks-2.md W4)
    comparten esta funcion en vez de repetir la derivacion de la clave de
    firma -- las dos instancias resultantes apuntan al mismo directorio y
    comparten `signing_key`, asi que un preview firmado por una lo verifica
    la otra sin estado compartido en memoria."""
    return LocalAssetStorage(
        settings.creative_asset_storage_dir,
        signing_key=derive_key(
            settings.session_secret.get_secret_value().encode(),
            _CREATIVE_PREVIEW_SIGNING_KEY_INFO,
        ),
        clock=container.clock,
    )


def _build_reference_data_tool_services(container: Container) -> ReferenceDataToolServices:
    """004 tasks-2.md R3/R4 (historia 18): `BrokerReferenceDataPort` implementa
    a la vez `MetaReferenceDataPort`/`GoogleReferenceDataPort` -- mismo
    `container.ads_platform_port` que `mcp_gaql_read_port` ya usa para
    `list_google_conversion_actions`/`search_google_constants` (GAQL, sin
    op nueva); los dos clientes de socket propios (`meta_reference_read`/
    `google_reference_read`) son las unicas ops nuevas de este puerto."""
    socket_path = container.settings.broker_socket_path
    port = BrokerReferenceDataPort(
        MetaReferenceDataBrokerClient(socket_path),
        GoogleKeywordIdeaBrokerClient(socket_path),
        container.ads_platform_port,
        container.session_factory,
    )
    return ReferenceDataToolServices(meta=port, google=port)


def _build_passthrough_tool_services(container: Container) -> PassthroughToolServices:
    """R5 (historia 19): `get_meta_graph`, unica lectura cuya politica de
    alcance vive en `mcp.domain.meta_graph_path` (S-1, security-engineer)."""
    return PassthroughToolServices(
        graph=BrokerGraphPassthroughPort(
            GraphPassthroughBrokerClient(container.settings.broker_socket_path),
            container.session_factory,
            container.clock,
        )
    )


def _build_competitor_tool_services(container: Container) -> CompetitorToolServices:
    """R7 (historias 21-23): `search_competitor_ads` sobre la Biblioteca de
    Anuncios de Meta; `get_competitor_links` no necesita puerto (puro)."""
    return CompetitorToolServices(
        competitor_research=BrokerCompetitorResearchPort(
            MetaAdLibraryBrokerClient(container.settings.broker_socket_path),
            container.session_factory,
        )
    )


def _build_company_tool_services(container: Container) -> CompanyToolServices:
    """R2/R8 (historias 12/24): `get_crm_summary`/`list_top_performing_ads`
    sobre lecturas SQL propias, sin credencial de plataforma."""
    return CompanyToolServices(
        crm_summary=SqlCrmSummaryReadPort(container.session_factory, container.clock),
        top_performing_ads=SqlTopPerformingAdsReadPort(container.session_factory, container.clock),
    )


_KIT_PREVIEW_SIGNING_KEY_INFO = b"safent-ads/kit-preview/v1"


def _build_kit_tool_services(container: Container, settings: ApiSettings) -> KitToolServices:
    """Kit de marketing del negocio (encargo del dueno, 14-sep): `store=None`
    si `ADS_KIT_DIR` no esta fijado -- las tres herramientas siguen
    registradas y reportan KIT_NOT_CONFIGURED, nunca fallan el arranque.
    Clave de firma propia (HKDF, mismo criterio que `_build_creative_asset_
    store`), nunca compartida con el almacen de creatividades."""
    if settings.kit_dir is None:
        return KitToolServices(
            store=None, public_base_url=settings.public_base_url, brand_name=settings.brand_name
        )
    store = LocalKitStore(
        settings.kit_dir,
        signing_key=derive_key(
            settings.session_secret.get_secret_value().encode(), _KIT_PREVIEW_SIGNING_KEY_INFO
        ),
        clock=container.clock,
    )
    return KitToolServices(
        store=store, public_base_url=settings.public_base_url, brand_name=settings.brand_name
    )


def _include_kit_preview_router(app: FastAPI, container: Container, settings: ApiSettings) -> None:
    """`GET /api/v1/kit-previews/{key}` (get_kit_file): ausente si
    `ADS_KIT_DIR` no esta fijado -- ni ruta ni almacen, mismo criterio que
    el panel sin `panel_dist_dir` (`_mount_panel_spa`). Funcion propia (no
    inline en `create_app`) solo para mantener bajo el tope de sentencias
    de esa funcion (Clean Code), nunca para cambiar el orden de montaje."""
    store = _build_kit_tool_services(container, settings).store
    if store is not None:
        app.include_router(build_kit_preview_router(store))


def _include_onboarding_router(app: FastAPI, settings: ApiSettings) -> None:
    """`GET /api/v1/onboarding` (029 T022): estado agregado que el puente
    `/ads/` de Safent lee para distinguir `unauthorized` de `no_accounts`.
    Funcion propia (no inline en `create_app`), mismo motivo que
    `_include_kit_preview_router`/`_include_cloudflare_connection_router`:
    mantener `create_app` bajo el tope de sentencias (Clean Code)."""
    from safent_ads.accounts.presentation.onboarding_rest import (  # noqa: PLC0415
        build_onboarding_router,
    )

    app.include_router(build_onboarding_router(settings))


def _include_cloudflare_connection_router(
    app: FastAPI, container: Container, settings: ApiSettings
) -> None:
    """`GET/POST/DELETE /api/v1/integrations/cloudflare*` (lane
    006-cloudflare-ui, owner decision): el propietario teclea el token de
    Cloudflare desde el panel -- mismo `session_factory`/`totp_enc_key`
    que `build_telegram_pairing_router`, la conexion se guarda cifrada en
    la BD de `ads-api`, nunca en `vendor.env`. Funcion propia (no inline
    en `create_app`) solo para mantener bajo el tope de sentencias de esa
    funcion (Clean Code), nunca para cambiar el orden de montaje."""
    app.include_router(
        build_cloudflare_connection_router(
            session_factory=container.session_factory,
            totp_enc_key=settings.totp_enc_key.get_secret_value(),
            fallback_token_configured=settings.cloudflare_api_token is not None,
            clock=container.clock,
        )
    )


def _build_cloudflare_tool_services(
    container: Container, settings: ApiSettings
) -> CloudflareToolServices:
    """Anadido del dueno (14-sep): conector propio de Cloudflare, sin pasar
    por `ads-broker` -- el token vive cifrado en la BD de `ads-api`
    (`cloudflare_connection`, 0050_cloudflare_connection, lane
    006-cloudflare-ui), no en `BrokerSettings`. `DynamicCloudflareService`
    resuelve ese token guardado por el panel primero, `CLOUDFLARE_API_
    TOKEN` de respaldo despues, en CADA llamada MCP (nunca uno fijado al
    arrancar el proceso: conectar desde el panel tiene que funcionar sin
    reiniciar `ads-api`). Sin ninguno de los dos, las cinco herramientas
    siguen registradas y `CLOUDFLARE_NOT_CONNECTED` lleva el enlace donde
    crear un token -- nunca fallan el arranque."""
    store = build_request_scoped_cloudflare_connection_store(
        container.session_factory, totp_enc_key=settings.totp_enc_key.get_secret_value()
    )
    fallback_token = (
        settings.cloudflare_api_token.get_secret_value()
        if settings.cloudflare_api_token is not None
        else None
    )
    return CloudflareToolServices(
        cloudflare=DynamicCloudflareService(
            store=store,
            fallback_token=fallback_token,
            allowed_zones=frozenset(settings.cloudflare_allowed_zones),
        ),
        connection_status=GetCloudflareConnectionStatus(
            store, fallback_token_configured=bool(fallback_token)
        ),
    )


def _build_creative_upload_tool_services(
    container: Container, settings: ApiSettings, creative_generation: CreativeGenerationToolServices
) -> CreativeUploadToolServices:
    """W4 (historia 13): `upload_creative_asset` reutiliza `ImportCreativeAsset.
    from_bytes` (gemelo de `UploadBrandAsset.from_bytes`) sobre el MISMO
    `LocalAssetStorage`/`briefs` que REST -- un solo almacen trazable.

    `asset_fetch`/`allowed_hosts` solo satisfacen el constructor de
    `ImportCreativeAsset`: el camino MCP nunca llama a `execute()` (D-2,
    "base64 unicamente, nunca URL libre") -- `allowed_hosts=frozenset()`
    dejaria ese camino fail-closed si alguna vez se invocara por error."""
    asset_store = _build_creative_asset_store(container, settings)
    import_creative_asset = ImportCreativeAsset(
        briefs=creative_generation.briefs,
        assets=creative_generation.assets,
        asset_fetch=HttpAssetFetcher(httpx.AsyncClient(follow_redirects=False)),
        asset_store=asset_store,
        allowed_hosts=frozenset(),
        clock=container.clock,
    )
    upload_port: CreativeUploadPort = ContainerCreativeUploadAdapter(
        import_creative_asset, asset_store
    )
    return CreativeUploadToolServices(
        briefs=creative_generation.briefs,
        upload_port=upload_port,
        public_base_url=settings.public_base_url,
    )


class _SqlDecisionAudit:
    """Implementa `DecisionAuditPort` (`mcp/presentation/dispatcher.py`)
    sobre `RecordDecision` (004 tasks.md A7): una fila de `decision_log` por
    llamada MCP, su propia transaccion -- nunca comparte la sesion del
    handler, para que una auditoria fallida no invalide un exito ya hecho."""

    def __init__(self, container: Container) -> None:
        self._container = container

    async def record_tool_call(
        self,
        *,
        caller_id: str,
        person_label: str,
        business_id: str,
        tool_name: str,
        permission: str,
        outcome: str,
        error_code: str | None,
        args_digest: str,
        duration_ms: int,
    ) -> None:
        async with self._container.session_factory() as session:
            await RecordDecision(SqlDecisionLogRepository(session)).execute(
                PendingDecision(
                    business_id=BusinessId.parse(business_id),
                    kind=DecisionKind.MCP_TOOL_CALL,
                    actor_kind=ActorKind.AGENT,
                    actor_id=caller_id,
                    payload={
                        "caller_person": caller_id,
                        "person_label": person_label,
                        "tool": tool_name,
                        "permission": permission,
                        "outcome": outcome,
                        "error_code": error_code,
                        "args_digest": args_digest,
                        "duration_ms": duration_ms,
                    },
                )
            )
            await session.commit()


def _build_caller_scope_resolver(
    settings: ApiSettings, container: Container, *, token_verifier: TokenVerifier
) -> CallerScopeResolverPort:
    """004 tasks.md A6/A10 (aclaracion del dueno): dos modos de primera
    clase, mutuamente excluyentes (`ApiSettings._require_exactly_one_mcp_
    authority_mode`). `ADS_SEAT_AUTHORITY_ENABLED=true` resuelve el puesto
    contra Enterprise (produccion/alojado). `ADS_SINGLE_OWNER_MODE=true` es
    el Safent local del dueno (motor Hermes): mismo bearer estatico de
    siempre, pero con alcance explicito -- nunca `None`
    (`SingleOwnerCallerScopeResolver`, A1).

    Spec 002 (mcp_oauth) tasks.md T014, fusion lane/003: con
    `ADS_MCP_OAUTH_ENABLED=true` la cadena empieza por
    `OAuthCallerScopeResolver`, que reconoce una concesion OAuth viva con
    el MISMO `CompositeTokenVerifier` que protege `GET /mcp/health`
    (threat-model.md C-48) y delega en el resolutor de puesto cualquier
    otro bearer -- el estatico `ADS_MCP_TOKEN` incluido."""
    seat_resolver: CallerScopeResolverPort
    if settings.seat_authority_enabled:
        trust = settings.seat_trust()
        seat_resolver = EnterpriseSeatCallerScopeResolver(EnterpriseSeatAuthority(trust))
    else:
        seat_resolver = SingleOwnerCallerScopeResolver(
            container.session_factory,
            expected_token=(
                settings.mcp_token.get_secret_value()
                if settings.mcp_static_token_enabled and settings.mcp_token is not None
                else None
            ),
        )
    if not settings.mcp_oauth_enabled:
        return seat_resolver
    return OAuthCallerScopeResolver(
        token_verifier=token_verifier,
        active_businesses=SqlActiveBusinessIds(container.session_factory),
        fallback=seat_resolver,
    )


def _build_mcp_registry_and_dispatcher(
    container: Container,
    settings: ApiSettings,
    creative_generation: CreativeGenerationToolServices,
) -> tuple[ToolRegistry, ToolDispatcher]:
    """Cablea `ToolRegistry`/`ToolDispatcher` (T044): todos los puertos de
    lectura son reales (`creative` cablea `SqlCreativeReadPort` desde el
    carril gap-creatives, el ultimo que quedaba placeholder). Las escrituras
    (US2/US3) van sobre `ContainerProposalWriteAdapter`
    (`composition/mcp_write_adapter.py`): unico puente entre `mcp`, que no
    puede importar `Container`, y el camino de escritura real.

    `creative_generation` llega ya construido desde `create_app` (no se
    fabrica aqui): lo comparte con `_build_creative_router` para que
    `generate_creative_assets` tenga una unica cola GPU serializada, nunca
    dos independientes para la misma superficie fisica (B-1,
    checklists/final-review.md).

    El registro devuelto es el COMPLETO (`aprobar`): `create_app`
    (004 tasks.md A6) lo reparte en tres con `registries_by_permission`."""
    ports = ReadModelPorts(
        business_directory=SqlMcpBusinessDirectory(container.session_factory),
        portfolio=container.mcp_portfolio_read_port,
        entity=container.mcp_entity_read_port,
        gaql=container.mcp_gaql_read_port,
        signal=container.mcp_signal_read_port,
        rule=container.mcp_rule_read_port,
        proposal=container.mcp_proposal_read_port,
        catalog=container.mcp_catalog_read_port,
        audit=container.mcp_audit_read_port,
        creative=SqlCreativeReadPort(container.session_factory),
        brand=SqlBrandReadPort(container.session_factory),
        # `capability`: unica sonda que esta lane cablea de verdad, porque
        # no depende de una sesion de BD por peticion -- solo `os.environ`.
        capability=EnvironmentCapabilityProbe(),
    )
    write_port = ContainerProposalWriteAdapter(container)
    experiment_services = _build_experiment_tool_services(container)
    search_terms_services = _build_search_terms_tool_services(container)
    opportunity_services = _build_opportunity_tool_services(container, settings)
    # 003-paquete-de-campana: `ADS_CAMPAIGN_PACKAGES_ENABLED` (settings.py)
    # gatea el registro -- `None` es la misma senal que ya usa
    # `build_default_registry` para el resto de modulos opcionales
    # (`_extend_with_optional_tool_modules`), no un `if` nuevo dentro de
    # `catalog.py`. Companion 0.2.21 se corta desde esta rama antes de que
    # exista la saga de publicacion del paquete (Meta falla con
    # `PLATFORM_NATIVE_INCOMPLETE`); con el flag apagado, `propose_campaign_
    # package` no aparece en ningun catalogo por permiso.
    package_services = (
        _build_package_tool_services(container, settings)
        if settings.campaign_packages_enabled
        else None
    )
    kit_services = _build_kit_tool_services(container, settings)
    registry = build_default_registry(
        ports,
        container.clock,
        write_port,
        experiment_services=experiment_services,
        search_terms_services=search_terms_services,
        opportunity_services=opportunity_services,
        economics_service=build_economics_query_service(container.session_factory, container.clock),
        offering_creation=CreateOffering(RequestScopedOfferingCreation(container.session_factory)),
        campaign_drafts=CampaignDraftStore(
            container.session_factory,
            container.clock,
            enabled_google_channels=settings.google_channels_enabled,
        ),
        optimization_service=_build_optimization_query_service(container),
        creative_generation_services=creative_generation,
        native_ads_services=NativeAdsToolServices(
            BrokerNativeAdsReadPort(
                NativeAdsBrokerClient(container.settings.broker_socket_path),
                container.session_factory,
            )
        ),
        connection_services=_build_connection_tool_services(container),
        reference_data_services=_build_reference_data_tool_services(container),
        passthrough_services=_build_passthrough_tool_services(container),
        competitor_services=_build_competitor_tool_services(container),
        company_services=_build_company_tool_services(container),
        creative_upload_services=_build_creative_upload_tool_services(
            container, settings, creative_generation
        ),
        package_services=package_services,
        cloudflare_services=_build_cloudflare_tool_services(container, settings),
        kit_services=kit_services,
        enabled_google_channels=settings.google_channels_enabled,
        brand_name=settings.brand_name,
    )
    quota = InMemoryQuota(
        clock=container.clock,
        writes_limit_per_minute=settings.mcp_quota_writes_per_minute,
        per_tool_limits={
            "get_meta_graph": settings.mcp_quota_get_meta_graph_per_minute,
            "search_competitor_ads": settings.mcp_quota_search_competitor_ads_per_minute,
            "get_google_keyword_ideas": settings.mcp_quota_get_google_keyword_ideas_per_minute,
            "upload_creative_asset": settings.mcp_quota_upload_creative_asset_per_minute,
        },
    )
    audit: DecisionAuditPort = _SqlDecisionAudit(container)
    dispatcher = ToolDispatcher(registry=registry, quota=quota, audit=audit)
    return registry, dispatcher


def _build_experiment_tool_services(container: Container) -> ExperimentToolServices:
    """T201 (profitability-engine.md §4/§6): las 4 tools de experimentacion/
    calibracion sobre los adaptadores reales de `optimization/infrastructure/`
    -- `design_experiment` es puro (sin sesion, ver `DesignExperiment`)."""
    experiments = RequestScopedExperimentRepository(container.session_factory)
    return ExperimentToolServices(
        design=DesignExperiment(),
        propose=ProposeExperiment(
            proposals=RequestScopedExperimentProposals(container.session_factory, container.clock),
            experiments=experiments,
            clock=container.clock,
        ),
        status=GetExperimentStatus(experiments),
        calibration_report=GetCalibrationReport(
            RequestScopedCalibrationInputs(container.session_factory)
        ),
    )


def _build_search_terms_tool_services(container: Container) -> SearchTermsToolServices:
    """tool-surface.md §2.1 P2: `list_search_terms` sobre el mismo
    `AdsPlatformPort`/socket que `container.mcp_gaql_read_port` ya usa, con
    la plantilla versionada `broker/platforms/gaql/search_terms.gaql`
    (`load_gaql_template`, T162 follow-up); `get_budget_envelope` sobre las
    piezas de `rules.application.read_models.caps_and_pacing` (sin sesion
    propia por peticion mas que la que ya abre cada puerto SQL)."""
    return SearchTermsToolServices(
        search_terms=BrokerSearchTermReadPort(
            container.ads_platform_port,
            container.session_factory,
            container.clock,
            query_template=load_gaql_template("search_terms"),
        ),
        budget_envelope=SqlBudgetEnvelopeReadPort(container.session_factory, container.clock),
    )


def _build_opportunity_tool_services(
    container: Container, settings: ApiSettings
) -> OpportunityToolServices:
    """T114: `propose_campaign`/`list_opportunities` sobre los adaptadores
    reales de `opportunities/infrastructure/` -- mismo patron de sesion por
    llamada que `_build_experiment_tool_services`."""
    return OpportunityToolServices(
        propose_campaign=ProposeCampaign(
            offerings=RequestScopedOfferingExists(container.session_factory),
            accounts=RequestScopedActiveAccountLookup(container.session_factory),
            daily_caps=RequestScopedAccountDailyCap(container.session_factory),
            campaign_proposals=RequestScopedCampaignProposals(
                container.session_factory, container.clock
            ),
            clock=container.clock,
            enabled_google_channels=settings.google_channels_enabled,
        ),
        list_opportunities=ListOpportunities(
            opportunities=RequestScopedOpenOpportunities(container.session_factory)
        ),
    )


class _PackageBudgetEnvelopeAdapter:
    """Adapta `SqlBudgetEnvelopeReadPort` (ya construido para
    `get_budget_envelope`, tool-surface.md §2.1) a la forma propia de
    `packages.application.ports.BudgetEnvelopeReadPort` -- `packages` no
    puede importar `mcp` (crearia un ciclo, `mcp` ya depende de `packages`
    para montar `propose_campaign_package`), asi que la traduccion vive
    aqui, en `composition`, que si puede depender de los dos."""

    def __init__(self, inner: SqlBudgetEnvelopeReadPort) -> None:
        self._inner = inner

    async def get_budget_envelope(self, business_id: str) -> PackageBudgetEnvelope:
        envelope = await self._inner.get_budget_envelope(business_id)
        return PackageBudgetEnvelope(
            monthly_cap_minor=envelope.monthly_cap_minor,
            spent_month_to_date_minor=envelope.spent_month_to_date_minor,
            headroom_minor=envelope.headroom_minor,
            currency=envelope.currency,
            reason=envelope.reason,
        )


def _build_package_tool_services(
    container: Container, settings: ApiSettings
) -> PackageToolServices:
    """T040/T021: `propose_campaign_package` sobre los adaptadores reales de
    `packages/infrastructure/` -- mismo patron de sesion por llamada que
    `_build_opportunity_tool_services`. `publish_as_lookup` es
    `NullPublishAsLookup` a proposito (gap documentado en el informe de la
    rama: no existe todavia ninguna pagina de Meta conectada en el esquema
    de `accounts`) -- Meta falla con `PLATFORM_NATIVE_INCOMPLETE` hasta que
    esa infraestructura exista; Google no la necesita."""
    packages = RequestScopedCampaignPackages(container.session_factory)
    return PackageToolServices(
        propose_campaign_package=ProposeCampaignPackage(
            packages=packages,
            offerings=RequestScopedPackageOfferingExists(container.session_factory),
            accounts=RequestScopedPackageActiveAccountLookup(container.session_factory),
            daily_caps=RequestScopedPackageAccountDailyCap(container.session_factory),
            budget_envelope=_PackageBudgetEnvelopeAdapter(
                SqlBudgetEnvelopeReadPort(container.session_factory, container.clock)
            ),
            publish_as_lookup=NullPublishAsLookup(),
            landing_policy=RequestScopedLandingDomainPolicy(container.session_factory),
            creative_lookup=PackageCreativeAssetLookup(
                RequestScopedCreativeAssetRepository(container.session_factory),
                _build_creative_asset_store(container, settings),
            ),
            clock=container.clock,
            enabled_google_channels=settings.google_channels_enabled,
        ),
        packages=packages,
        enabled_google_channels=settings.google_channels_enabled,
    )


def _build_optimization_query_service(container: Container) -> OptimizationQueryService:
    """B-1 (checklists/final-review.md): `get_marginal_roas`/`diagnose_entity`/
    `simulate_spend_change`/`propose_reallocation_plan` sobre los mismos 6
    adaptadores SQL reales que `optimization/presentation/rest.py::
    build_optimization_router_over_sql` ya usa para REST -- dos instancias
    de `OptimizationQueryService` (REST y MCP) son seguras porque cada
    `RequestScoped*` abre y cierra su propia sesion por llamada, sin estado
    compartido entre ellas."""
    return OptimizationQueryService(
        marginal_estimates=RequestScopedMarginalEstimates(container.session_factory),
        diagnosis_metrics=RequestScopedDiagnosisMetrics(container.session_factory, container.clock),
        response_curves=RequestScopedResponseCurves(container.session_factory),
        contribution_margins=RequestScopedContributionMargins(
            container.session_factory, container.clock
        ),
        reallocation_candidates=RequestScopedReallocationCandidates(container.session_factory),
        reallocation_proposals=RequestScopedReallocationProposals(
            container.session_factory, container.clock
        ),
        clock=container.clock,
    )


def _build_broker_image_renderers(
    container: Container, asset_store: LocalAssetStorage
) -> dict[RendererName, ImageRendererPort]:
    """`BrokerImageRenderer` por cada renderizador de imagen que el broker
    PUEDE tener configurado (`FAL_API_KEY`/`OPENAI_API_KEY` viven solo en
    `BrokerSettings`, threat-model.md C-29 -- `ads-api` nunca las ve, asi
    que no puede saber cual esta activa sin preguntarle). Los dos se
    registran siempre, sin sondear al broker al arrancar: si una clave no
    esta configurada, ESE render falla con `IMAGE_RENDERER_NOT_CONFIGURED`
    y `GenerateCreativeAssets._render_first_available` ya trata cualquier
    fallo de un candidato como "probar el siguiente" (creative-port.md,
    correccion del propietario 2026-09-09) -- el mismo comportamiento que
    un renderizador local sin GPU disponible, sin necesitar una op de
    capacidad (`list_image_renderers`) ni un sondeo sincrono en el arranque
    de `create_app`."""
    client = ImageRenderBrokerClient(container.settings.broker_socket_path)
    return {
        name: BrokerImageRenderer(name, client, asset_store)
        for name in (RendererName.FLUX2_KLEIN_9B, RendererName.GPT_IMAGE_1_5)
    }


def _build_creative_generation_services(
    container: Container, settings: ApiSettings
) -> CreativeGenerationToolServices:
    """`generate_creative_assets`/`run_creative_policy_check`: mismos
    adaptadores que `_build_creative_router` usaba inline hasta B-1
    (checklists/final-review.md) -- se construyen UNA vez aqui y
    `create_app` reparte el resultado entre REST y MCP para que
    `InProcessGpuQueue` sea de verdad una unica cola serializada."""
    briefs = RequestScopedCreativeBriefRepository(container.session_factory)
    assets = RequestScopedCreativeAssetRepository(container.session_factory)
    jobs = RequestScopedCreativeJobRepository(container.session_factory)
    asset_store = _build_creative_asset_store(container, settings)
    return CreativeGenerationToolServices(
        briefs=briefs,
        assets=assets,
        generate_creative_assets=GenerateCreativeAssets(
            briefs=briefs,
            jobs=jobs,
            assets=assets,
            image_renderers=_build_broker_image_renderers(container, asset_store),
            renderer_selector=RendererSelector(),
            gpu_lease=InProcessGpuQueue(container.clock),
        ),
        run_policy_check=RunPolicyCheck(LocalPolicyChecker(assets), assets),
    )


# --- end lane: surface ---


def _build_website_brand_discovery(
    storage: LocalBrandAssetStorage, clock: Clock
) -> WebsiteBrandExtractor:
    """`WebsiteBrandDiscoveryPort` real (threat-model.md C-11/C-12,
    checklists/website-brand-extractor-review.md, verdict "WIRE AFTER
    FIXES"): hace I/O de red de verdad contra el host que el propietario
    ha confirmado -- ya reviso `security-engineer` la superficie completa
    (SSRF fijado a la IP validada, tope global de bytes, fecha limite de
    rastreo, saneado de SVG, bomba de descompresion) antes de este cambio.
    `follow_redirects=False` explicito: si se hereda `True` del cliente,
    el bucle manual de revalidacion salto a salto de
    `WebsiteBrandExtractor._request_bytes_safely` nunca ve la redireccion
    y el control SSRF por salto desaparece. `enable_playwright_pass=False`
    explicito: el paso opcional con Playwright puentea todos los
    controles SSRF (resuelve DNS, sigue redirecciones y carga subrecursos
    por su cuenta) y queda fuera de este cableado a proposito."""
    http_client = httpx.AsyncClient(follow_redirects=False)
    return WebsiteBrandExtractor(
        http_client,
        storage,
        clock,
        enable_playwright_pass=False,
    )


def _build_brand_router(container: Container, asset_storage_dir: Path) -> APIRouter:
    """`GET /brand`, `GET /brand/assets`, `POST /brand/assets`,
    `GET /brand/assets/{asset_id}/preview`, `POST /brand/discover`,
    `GET /brand/draft`, `POST /brand/confirm`, `PUT /brand/claims` sobre
    adaptadores reales:
    `RequestScopedBrandKitRepository`/
    `RequestScopedBrandDiscoveryDraftRepository` (SQL, una sesion por
    peticion -- mismo patron que `RequestScopedPanelReadPort`),
    `LocalBrandAssetStorage` (disco local) y `WebsiteBrandExtractor`
    (`_build_website_brand_discovery`, unica superficie de red nueva de
    este router)."""
    brand_kits = RequestScopedBrandKitRepository(container.session_factory)
    drafts = RequestScopedBrandDiscoveryDraftRepository(container.session_factory)
    storage = LocalBrandAssetStorage(asset_storage_dir)
    return build_brand_router(
        get_brand_kit=GetBrandKit(brand_kits),
        list_brand_assets=ListBrandAssets(brand_kits),
        ingest_brand_from_website=IngestBrandFromWebsite(
            discovery=_build_website_brand_discovery(storage, container.clock),
            drafts=drafts,
            brand_kits=brand_kits,
            clock=container.clock,
        ),
        get_brand_draft=GetBrandDraft(drafts),
        confirm_brand_draft=ConfirmBrandDraft(
            drafts=drafts, brand_kits=brand_kits, clock=container.clock
        ),
        upload_brand_asset=UploadBrandAsset(drafts=drafts, storage=storage, clock=container.clock),
        get_brand_asset_preview=GetBrandAssetPreview(
            brand_kits=brand_kits, drafts=drafts, storage=storage
        ),
        update_brand_claims=UpdateBrandClaims(
            brand_kits=brand_kits,
            clock=container.clock,
            decision_recorder=SqlBrandClaimsDecisionRecorder(container.session_factory),
        ),
    )


def _build_creative_router(
    container: Container,
    settings: ApiSettings,
    creative_generation: CreativeGenerationToolServices,
) -> APIRouter:
    """`GET /creatives`, `GET /creatives/{asset_id}`, `POST /creative-jobs`,
    `GET /creative-jobs/{job_id}`, `GET /creative-previews/{key}` (bytes
    servidos de `signed_preview_url`, gap-creative-preview),
    `POST /creatives/{asset_id}/policy-check`,
    `POST /creatives/{asset_id}/reject`, `POST /creatives/{asset_id}/regenerate`,
    `POST /creatives/{asset_id}/propose-publication` sobre adaptadores
    reales: `RequestScopedCreative*Repository` (SQL, 0021_creative_review),
    `LocalAssetStorage` (disco local, mismo patron que
    `LocalBrandAssetStorage`) y `ContainerCreativeProposalGateway` (unica
    puerta hacia `proposals`, reutiliza
    `ContainerProposalWriteAdapter.propose_creative_publication`).

    `briefs`/`assets`/`generate_creative_assets`/`run_policy_check` llegan
    ya construidos en `creative_generation` (compartidos con el `ToolRegistry`
    MCP via `_build_mcp_registry_and_dispatcher`, B-1 checklists/final-
    review.md): una unica `InProcessGpuQueue` para las dos superficies.
    `jobs` es local a REST (`GET /creative-jobs/{job_id}`, no forma parte
    de las 2 herramientas MCP de esta rama).

    `image_renderers` (`_build_broker_image_renderers`, lane 003) ejecuta
    sobre `ads-broker` via el op `render_image` -- `OPENAI_API_KEY`/
    `FAL_API_KEY` viven en `BrokerSettings` (threat-model.md C-29), `ads-api`
    nunca las ve. Sin GPU local (`RendererSelector()` por defecto deja
    `LOCAL_GPU` fuera del registro -- 4 caidas termicas de la DGX el
    9-sep-2026): la cascada solo tiene el broker como proveedor directo. Sin
    ninguna clave configurada en el broker, cada candidato falla con
    `IMAGE_RENDERER_NOT_CONFIGURED` y la cascada se agota igual que antes --
    `POST /creative-jobs`/`POST /creatives/{id}/regenerate` responden
    `409 CREATIVE_RENDERER_UNAVAILABLE` con el informe de capacidad,
    reportado, nunca fingido (correccion del propietario 2026-09-09)."""
    jobs = RequestScopedCreativeJobRepository(container.session_factory)
    asset_store = _build_creative_asset_store(container, settings)
    return build_creative_router(
        briefs=creative_generation.briefs,
        assets=creative_generation.assets,
        jobs=jobs,
        asset_store=asset_store,
        generate_creative_assets=creative_generation.generate_creative_assets,
        run_policy_check=creative_generation.run_policy_check,
        propose_creative=ProposeCreative(
            ContainerCreativeProposalGateway(container), creative_generation.assets
        ),
        clock=container.clock,
    )


def _build_mcp_health_router(
    container: Container,
    *,
    token_verifier: TokenVerifier,
    resource_metadata_url: str,
    canonical_resource: str,
) -> APIRouter:
    """`GET /mcp/health` (companion contract). Spec 002 (mcp_oauth)
    tasks.md T014, threat-model.md C-48: el mismo `TokenVerifier` compuesto
    que protege `/mcp` -- nunca un segundo camino de verificacion -- y las
    sesiones de `container.session_factory`, mismo patron que
    `_build_brand_router`. M1 de la revision de seguridad (16-sep): exige
    ademas `ads:read` y el `resource` canonico, igual que `RequireAuthMiddleware`/
    `BearerAuthBackend` del SDK ya hacen para `/mcp`."""
    get_health_status = GetHealthStatus(
        accounts=SqlAccountLinkStatusPort(container.session_factory),
        database=SqlDatabaseHealthPort(container.session_factory),
    )
    return build_mcp_health_router(
        get_health_status=get_health_status,
        token_verifier=token_verifier,
        resource_metadata_url=resource_metadata_url,
        required_scope=OAuthScope.READ.value,
        canonical_resource=canonical_resource,
    )


@dataclass(frozen=True, slots=True)
class _McpOAuthWiring:
    """Lo que `/mcp`, `GET /mcp/health` y (si `mcp_oauth_enabled`) las
    rutas del AS necesitan del mismo cableado -- un unico lugar que decide
    la resource indicator canonica, en vez de recalcularla en tres sitios."""

    auth_settings: AuthSettings
    token_verifier: TokenVerifier
    resource_metadata_url: str
    resource: str
    oauth_provider: SdkOAuthProvider | None
    token_hasher: Sha256TokenHasher
    token_factory: SecretsOpaqueTokenFactory


def _build_mcp_oauth_wiring(container: Container, settings: ApiSettings) -> _McpOAuthWiring:
    """Spec 002 (mcp_oauth) tasks.md T013/T014. `session_factory=None`
    (composite verifier) apaga la rama OAuth sin banderas dentro del
    metodo (threat-model.md C-53): con `ADS_MCP_OAUTH_ENABLED=false`, `/mcp`
    y `/mcp/health` siguen protegidos por `auth=`/`token_verifier=` (mismo
    camino unico de verificacion, C-48) pero nunca abren una sesion de BD
    para el token presentado, y `oauth_provider` sale `None` para que
    `create_app` no monte las rutas del AS (plan.md "Cableado exacto")."""
    if settings.mcp_static_token_enabled:
        # M6 de la revision de seguridad (16-sep): el bearer estatico ya no
        # esta activado por defecto (threat-model.md C-53) -- encenderlo
        # deja rastro en el arranque, no solo en `.env`.
        logger.warning("mcp_static_token_enabled_at_startup")
    resource = f"{settings.public_base_url}{MCP_ENDPOINT_PATH}"
    token_hasher = Sha256TokenHasher()
    token_factory = SecretsOpaqueTokenFactory()
    token_verifier = _build_composite_token_verifier(
        container, settings, resource=resource, token_hasher=token_hasher
    )
    auth_settings = _build_mcp_auth_settings(settings, resource=resource)
    oauth_provider = (
        SdkOAuthProvider(
            session_factory=lambda: SqlOAuthSession(container.session_factory, container.clock),
            id_generator=container.id_generator,
            clock=container.clock,
            token_hasher=token_hasher,
            token_factory=token_factory,
            public_base_url=settings.public_base_url,
        )
        if settings.mcp_oauth_enabled
        else None
    )
    resource_metadata_url = str(build_resource_metadata_url(AnyHttpUrl(resource)))
    return _McpOAuthWiring(
        auth_settings=auth_settings,
        token_verifier=token_verifier,
        resource_metadata_url=resource_metadata_url,
        resource=resource,
        oauth_provider=oauth_provider,
        token_hasher=token_hasher,
        token_factory=token_factory,
    )


def _build_composite_token_verifier(
    container: Container,
    settings: ApiSettings,
    *,
    resource: str,
    token_hasher: Sha256TokenHasher,
) -> CompositeTokenVerifier:
    """I9 de la revision de seguridad (16-sep): la rama OAuth (`session_factory`)
    y la estatica (`static_token`) del unico verificador que protege `/mcp`
    y `GET /mcp/health` (threat-model.md C-48), cada una apagable por su
    propio conmutador sin banderas dentro del cuerpo del metodo (C-53)."""
    oauth_session_factory = (
        (lambda: SqlOAuthSession(container.session_factory, container.clock))
        if settings.mcp_oauth_enabled
        else None
    )
    return CompositeTokenVerifier(
        session_factory=oauth_session_factory,
        token_hasher=token_hasher,
        clock=container.clock,
        static_token=(
            settings.mcp_token.get_secret_value()
            if settings.mcp_static_token_enabled and settings.mcp_token is not None
            else None
        ),
        resource=resource,
    )


def _build_mcp_auth_settings(settings: ApiSettings, *, resource: str) -> AuthSettings:
    """I9: `AuthSettings` del SDK que protege `/mcp` -- `required_scopes`
    (`ads:read`, C-47) y las opciones de DCR/revocacion que
    `mcp_oauth/presentation/routes.py::build_oauth_routes` reutiliza para
    los metadatos publicados en `/.well-known/*` (C-49)."""
    return AuthSettings(
        issuer_url=oauth_issuer_url(settings.public_base_url),
        resource_server_url=AnyHttpUrl(resource),
        required_scopes=[OAuthScope.READ.value],
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[OAuthScope.READ.value, OAuthScope.PROPOSE.value],
            # L2 de la revision de seguridad (16-sep, minimo privilegio): un
            # cliente DCR que no pide alcance explicito recibe SOLO lectura
            # -- `ads:propose` (escribir propuestas) hay que pedirlo, nunca
            # cae por omision. `valid_scopes` (arriba) sigue permitiendo
            # ambos cuando el cliente los solicita de verdad.
            default_scopes=[OAuthScope.READ.value],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )


def _register_mcp_oauth_surface(
    app: FastAPI, wiring: _McpOAuthWiring, settings: ApiSettings
) -> None:
    """I9: lo que `create_app` monta a partir de `_McpOAuthWiring`, aparte
    de `/mcp/health` y el transporte streamable-http (montados antes, por
    orden de registro -- C-52). `ADS_MCP_OAUTH_ENABLED=false` deja
    `/mcp`/`/mcp/health` exactamente como hoy (bearer estatico) pero SIN
    las rutas del AS -- plan.md "Ajustes". El router de consentimiento y
    el de "Agentes conectados" se montan SIEMPRE: son mutaciones del panel
    (sesion + CSRF + TOTP), nunca superficie del AS, y no hacen nada nuevo
    si nunca se crea una `AuthorizationRequest`/`Grant` porque
    `/authorize` esta apagado.

    Revision de seguridad (PR 44): `well_known_not_found_route()` se anade
    SIEMPRE, tambien con OAuth apagado -- `/.well-known/*` nunca debe caer
    en el catch-all de SPA (`_mount_panel_spa`) y responder 200 con el
    HTML del panel, este activado el AS o no."""
    if wiring.oauth_provider is not None:
        _route_oauth_endpoints(
            app,
            wiring.oauth_provider,
            public_base_url=settings.public_base_url,
            resource_name=settings.instance_name,
        )
    app.router.routes.append(well_known_not_found_route())
    app.include_router(
        build_consent_router(
            token_hasher=wiring.token_hasher,
            token_factory=wiring.token_factory,
            totp_enc_key=settings.totp_enc_key.get_secret_value(),
            public_base_url=settings.public_base_url,
            federated_available=settings.federated_login_active,
        )
    )
    app.include_router(
        build_grants_router(
            totp_enc_key=settings.totp_enc_key.get_secret_value(),
            federated_available=settings.federated_login_active,
        )
    )


def _route_oauth_endpoints(
    app: FastAPI, provider: SdkOAuthProvider, *, public_base_url: str, resource_name: str
) -> None:
    """Cuelga las rutas del AS (`/authorize`, `/token`, `/register`,
    `/revoke`, `/.well-known/*`) del router del PADRE -- el SDK las
    registraria dentro de la sub-app de `/mcp` si se le pasara
    `auth_server_provider` a `MCPServer`, e inalcanzables ahi (plan.md
    "Cableado exacto"). Se registran justo despues de `_route_mcp_transport`
    y antes de `_mount_panel_spa`/`harden_api` (C-52)."""
    app.router.routes.extend(
        build_oauth_routes(
            provider, public_base_url=public_base_url, resource_name=resource_name
        )
    )


# Sin equivalente en `ApiSettings` (a diferencia de `active_hours`, que si
# viene de `ADS_ACTIVE_HOURS`): ninguna pieza de `orchestration/` dispara
# todavia a una hora fija del digest (ver docstring de
# `settings.infrastructure.sql_settings_repository`). Hora en punto
# razonable dentro de una jornada laboral tipica, documentada como valor
# por defecto arbitrario -- no una decision de producto.
_DEFAULT_DIGEST_HOUR = DigestHour(hour=9)


def _build_settings_router(settings: ApiSettings, container: Container) -> APIRouter:
    """`GET/PUT /settings` (contracts/rest-api.md §Ajustes) sobre
    `RequestScopedSettingsRepository`: mismo patron `session_factory` por
    llamada que `_build_brand_router`/`_build_mcp_health_router`."""
    start, end = settings.active_hours.split("-")
    default_active_hours = ActiveHours.parse(start=start, end=end)
    repository = RequestScopedSettingsRepository(
        container.session_factory,
        default_active_hours=default_active_hours,
        default_digest_hour=_DEFAULT_DIGEST_HOUR,
    )
    return build_settings_router(GetSettings(repository), UpdateSettings(repository))


def _render_embedded_index_html(
    index_html: str, *, prefix: str, instance_identity: InstanceIdentity
) -> str:
    """Serve one build at root or /ads without executable inline configuration.

    The strict CSP stays intact; metadata determines routing, and explicit
    same-origin asset URLs work on both landing pages and deep links.

    `instance_identity` (contracts/instance-identity.d.ts): fills the
    `__ADS_INSTANCE_NAME__`/`__ADS_PANEL_HOST__` placeholders that
    `panel/index.html` declares (`<title>`, no-JS fallback) and adds the
    two `<meta>` tags `instanceIdentity()` reads client-side -- same
    non-executable channel that already injects the embedded prefix below.
    """
    # CSP deliberately forbids inline scripts and <base>. Use non-executable
    # metadata and rewrite only known Vite asset references, not arbitrary URLs.
    if prefix not in {"", "/ads"}:
        raise ValueError("Unsupported panel prefix")
    escaped_name = html.escape(instance_identity.name, quote=True)
    escaped_host = html.escape(instance_identity.panel_host, quote=True)
    injected = (
        f'<meta name="safent-ads-base-path" content="{prefix}">\n'
        f'    <meta name="ads-instance-name" content="{escaped_name}">\n'
        f'    <meta name="ads-panel-host" content="{escaped_host}">'
    )
    rendered = re.sub(
        r"\b(src|href)=([\"'])(?:\./|/)?assets/",
        lambda match: f"{match[1]}={match[2]}{prefix}/assets/",
        index_html,
    )
    placeholders = {"__ADS_INSTANCE_NAME__": escaped_name, "__ADS_PANEL_HOST__": escaped_host}
    rendered = re.sub(
        "|".join(re.escape(placeholder) for placeholder in placeholders),
        lambda match: placeholders[match[0]],
        rendered,
    )
    return rendered.replace("<head>", f"<head>\n    {injected}", 1)


class _PanelSpaRoute(APIRoute):
    """El fallback visual no debe casar rutas API/MCP, ni parcialmente.

    De lo contrario, un POST a una API ausente pasa de 404 a 405 cuando
    existe el build, y un GET puede recibir index.html en lugar de JSON.
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        match, child_scope = super().matches(scope)
        # El router ya ha resuelto root_path (incluido el prefijo /ads).
        path = child_scope.get("path_params", {}).get("full_path", "")
        if path in {"api", "mcp"} or path.startswith(("api/", "mcp/")):
            return Match.NONE, {}
        return match, child_scope


# Perf (measured 16-sep on the production instance, item 1): Vite hashes every
# file it emits under `dist/assets/` on content change, so those responses
# are safe to cache forever; anything else in `dist/` (favicon, manifest...)
# keeps `no-store` like `index.html` because a redeploy can change its
# bytes at the same path.
_ASSET_DIR_PREFIX = "assets/"
_ASSET_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
_STATIC_NO_STORE_CACHE_CONTROL = "no-store"


def _static_asset_cache_control(full_path: str) -> str:
    if full_path.startswith(_ASSET_DIR_PREFIX):
        return _ASSET_IMMUTABLE_CACHE_CONTROL
    return _STATIC_NO_STORE_CACHE_CONTROL


def _mount_panel_spa(app: FastAPI, dist_dir: Path, instance_identity: InstanceIdentity) -> None:
    """Sirve `panel/dist/` (React/Vite, compilado aparte) en `/` con
    fallback de SPA: cualquier ruta que no sea un fichero real del build
    devuelve `index.html` (con el prefijo empotrado y la identidad de la
    instancia inyectados, ver `_render_embedded_index_html`) para que el
    router del lado cliente decida. Se registra el ultimo de todos los
    mounts/routers de este modulo a proposito -- Starlette resuelve por
    orden de registro, asi que `/api/v1/*`/`/mcp` siguen ganando aunque
    coincidan con este catch-all. Ausente en desarrollo sin build del
    panel: no es un fallo de arranque."""
    resolved_dist_dir = dist_dir.resolve()
    if not resolved_dist_dir.is_dir():
        logger.warning("panel_dist_not_found", path=str(resolved_dist_dir))
        return
    index_path = resolved_dist_dir / "index.html"
    if not index_path.is_file():
        logger.warning("panel_dist_missing_index_html", path=str(resolved_dist_dir))
        return
    index_html_template = index_path.read_text(encoding="utf-8")

    async def panel_spa(full_path: str, request: Request) -> Response:
        candidate = (resolved_dist_dir / full_path).resolve()
        if (
            full_path
            and candidate != index_path
            and candidate.is_file()
            and candidate.is_relative_to(resolved_dist_dir)
        ):
            return FileResponse(
                candidate,
                headers={"Cache-Control": _static_asset_cache_control(full_path)},
            )
        # `ForwardedPrefixMiddleware` ya validó y default-denegó el prefijo
        # (composition/api.py, contracts/sso.md §7 T-1) -- se lee tal cual.
        prefix = request.scope.get("root_path", "")
        return HTMLResponse(
            _render_embedded_index_html(
                index_html_template, prefix=prefix, instance_identity=instance_identity
            ),
            headers={"Cache-Control": "no-store"},
        )

    app.router.add_api_route(
        "/{full_path:path}",
        panel_spa,
        methods=["GET"],
        include_in_schema=False,
        route_class_override=_PanelSpaRoute,
    )


@dataclass(frozen=True, slots=True)
class _McpSurface:
    """Todo lo que `create_app` necesita del transporte MCP: los tres
    sub-apps por permiso, el enrutador que los precede, el resolutor de
    alcance (que el `lifespan` cierra) y el cableado OAuth compartido con
    `GET /mcp/health` y las rutas del AS."""

    router: SeatCredentialRouter
    apps: dict[Permission, Starlette]
    caller_scope_resolver: CallerScopeResolverPort
    oauth_wiring: _McpOAuthWiring


def _build_mcp_surface(
    container: Container,
    settings: ApiSettings,
    creative_generation: CreativeGenerationToolServices,
    *,
    caller_scope_resolver: CallerScopeResolverPort | None,
) -> _McpSurface:
    """004 tasks.md A6: tres `MCPServer` (ver/proponer/aprobar) sobre el
    MISMO `ToolDispatcher`/`Container`; `SeatCredentialRouter` resuelve la
    credencial una vez por peticion y delega en el sub-app del permiso.

    Spec 002 (mcp_oauth) tasks.md T013/T014: unico cableado de autenticacion
    para `/mcp` y `GET /mcp/health` (threat-model.md C-48). Tras la fusion
    con lane/003, `auth=`/`token_verifier=` del SDK ya no protegen `/mcp`
    (la sub-app cuelga detras de `SeatCredentialRouter`): el mismo
    `CompositeTokenVerifier` se usa desde la cadena de resolutores y desde
    `GET /mcp/health`, un unico camino de verificacion."""
    registry, dispatcher = _build_mcp_registry_and_dispatcher(
        container, settings, creative_generation
    )
    oauth_wiring = _build_mcp_oauth_wiring(container, settings)
    resolver = caller_scope_resolver or _build_caller_scope_resolver(
        settings, container, token_verifier=oauth_wiring.token_verifier
    )
    mcp_servers = build_mcp_servers(
        registries=registries_by_permission(registry),
        dispatcher=dispatcher,
        instructions=build_mcp_instructions(
            brand_name=settings.brand_name, panel_url=settings.public_base_url
        ),
    )
    router, apps = build_mcp_asgi_apps(
        mcp_servers,
        caller_scope_resolver=resolver,
        public_base_url=settings.public_base_url,
        extra_allowed_hosts=frozenset(settings.mcp_extra_allowed_hosts),
        resource_metadata_url=oauth_wiring.resource_metadata_url,
    )
    return _McpSurface(
        router=router, apps=apps, caller_scope_resolver=resolver, oauth_wiring=oauth_wiring
    )


# Umbral de `GZipMiddleware` (item 1 del perf 16-sep): por debajo de esto
# comprimir cuesta mas CPU que ancho de banda ahorra.
_GZIP_MINIMUM_SIZE_BYTES = 1024


def create_app(
    settings: ApiSettings | None = None,
    *,
    caller_scope_resolver: CallerScopeResolverPort | None = None,
) -> FastAPI:
    """Construye la app FastAPI de `ads-api`. Sin argumentos, lee `ApiSettings`
    del entorno (uso en produccion via `--factory`); con `settings`, permite
    inyeccion explicita en tests. `caller_scope_resolver` es la misma clase
    de costura: los tests de extremo a extremo de `/mcp` (004 tasks.md A6)
    sustituyen la introspeccion real contra Enterprise por un doble, sin
    tocar el resto del cableado."""
    configure_logging()
    resolved_settings = settings or ApiSettings()  # type: ignore[call-arg]
    # `TELEGRAM_BOT_TOKEN`/`TELEGRAM_OWNER_CHAT_IDS` son opcionales al
    # arrancar (composition/settings.py): una instalacion limpia sin bot
    # de Telegram todavia arranca, pero lo dice una vez en el log en vez
    # de quedar en silencio (`_telegram_configured` mas abajo hace la misma
    # comprobacion para `/api/v1/health/deep`).
    if not resolved_settings.telegram_bot_token.get_secret_value() or (
        not resolved_settings.telegram_owner_chat_ids
    ):
        logger.warning("telegram_not_configured")
    container = Container.build(resolved_settings)
    if resolved_settings.managed_central:
        return create_managed_app(container)
    # Compartido entre el `ToolRegistry` MCP y el router REST de `creative`
    # (B-1, checklists/final-review.md): una unica `InProcessGpuQueue`.
    creative_generation = _build_creative_generation_services(container, resolved_settings)

    # --- lane: surface ---
    mcp_surface = _build_mcp_surface(
        container,
        resolved_settings,
        creative_generation,
        caller_scope_resolver=caller_scope_resolver,
    )
    oauth_wiring = mcp_surface.oauth_wiring
    # --- end lane: surface ---

    app = FastAPI(
        title="safent-ads",
        version=__version__,
        lifespan=_build_lifespan(container, mcp_surface.apps, mcp_surface.caller_scope_resolver),
    )
    app.state.container = container

    _mount_health(app)
    # `/mcp/health` se registra ANTES de la ruta de `/mcp`: Starlette
    # resuelve por orden de registro (mismo patron que `_mount_panel_spa`
    # mas abajo), asi que esta ruta exacta gana sobre la del transporte
    # streamable-http.
    app.include_router(
        _build_mcp_health_router(
            container,
            token_verifier=oauth_wiring.token_verifier,
            resource_metadata_url=oauth_wiring.resource_metadata_url,
            canonical_resource=oauth_wiring.resource,
        )
    )
    _route_mcp_transport(app, mcp_surface.router)

    # --- lane: mcp-oauth (cableado) ---
    _register_mcp_oauth_surface(app, oauth_wiring, resolved_settings)
    # --- end lane: mcp-oauth (cableado) ---

    # --- lane: secfound ---
    # Import local a proposito: mantiene todo el cambio de este carril
    # contenido dentro del bloque marcado, sin tocar la cabecera de imports
    # que otros carriles paralelos tambien puedan estar editando.
    from safent_ads.composition.api import harden_api  # noqa: PLC0415

    harden_api(app, resolved_settings, container)
    app.include_router(build_composio_internal_router(resolved_settings))
    # --- end lane: secfound ---
    # --- lane: surface ---
    app.include_router(build_panel_router(RequestScopedPanelReadPort(container.session_factory)))
    # 026 (tasks.md T007/T012): cuadro de mando, compuesto sobre los mismos
    # read models -- `RequestScopedCockpitReadModel` abre su propia sesion
    # por llamada, igual que `RequestScopedPanelReadPort`.
    app.include_router(
        build_cockpit_router(
            RequestScopedCockpitReadModel(container.session_factory, container.clock),
            clock=container.clock,
        )
    )
    # --- end lane: surface ---
    app.include_router(_build_brand_router(container, resolved_settings.brand_asset_storage_dir))
    app.include_router(_build_creative_router(container, resolved_settings, creative_generation))
    _include_kit_preview_router(app, container, resolved_settings)
    app.include_router(build_execution_router(container))
    # `rules`/`guardrails`: alta, listado, borrado suave, ensayo en seco y
    # lectura de guardarrailes (gap-accounts-rules) -- fuera de
    # `execution_rest.py` a proposito, ver `rules/presentation/rest.py`.
    app.include_router(build_rules_router(container))
    app.include_router(
        build_telegram_pairing_router(
            session_factory=container.session_factory,
            totp_enc_key=resolved_settings.totp_enc_key.get_secret_value(),
            allowlist_configured=bool(resolved_settings.telegram_owner_chat_ids),
            clock=container.clock,
            id_generator=container.id_generator,
        )
    )
    app.include_router(build_economics_read_router(container.session_factory))
    # Spec 027 T017 (contracts/crm-link.md §3): router propio, no toca la
    # firma de `EconomicsQueryService` (ver docstring de
    # `customer_value_rest.py`).
    app.include_router(build_customer_value_router(container.session_factory))
    # --- lane: economics-inputs-ui ---
    # `GET /offerings`/`PUT /offerings/{id}/economics` (T131/T132) y
    # `POST /conversions/{import,webhook,webhook-token}` (T220): numeros de
    # margen y conversiones del CRM del propietario desde el panel, en vez
    # de `config/economics/<business>.yaml`/env (owner decision,
    # 0029_economics_inputs).
    app.include_router(build_offerings_router(container.session_factory))
    app.include_router(
        build_campaign_drafts_router(
            CampaignDraftStore(
                container.session_factory,
                container.clock,
                enabled_google_channels=resolved_settings.google_channels_enabled,
            )
        )
    )
    app.include_router(
        build_conversions_router(
            session_factory=container.session_factory,
            identity_salt=HkdfIdentitySalt(
                resolved_settings.session_secret.get_secret_value().encode()
            ),
            clock=container.clock,
        )
    )
    # --- end lane: economics-inputs-ui ---
    # Spec 027 (contracts/crm-link.md §2): borde de ingesta del puente
    # CRM->anuncios (`X-Bridge-Token`, tabla propia `crm_bridge_tokens` --
    # NUNCA `conversion_webhook_tokens`, dos superficies de confianza
    # distintas, ver docstring de `0033_crm_bridge_health`).
    app.include_router(
        build_crm_bridge_router(
            session_factory=container.session_factory,
            clock=container.clock,
        )
    )
    app.include_router(
        build_proposal_admin_router(
            container.session_factory,
            container.clock,
            enabled_google_channels=resolved_settings.google_channels_enabled,
        )
    )
    # 003-paquete-de-campana (T030/T031): mismo LocalAssetStorage que
    # `_build_creative_router`/`_build_creative_upload_tool_services`, un
    # solo almacen trazable para las tres superficies.
    #
    # H2 (revision de codigo, saga de publicacion 2026-09-15):
    # `ADS_CAMPAIGN_PACKAGES_ENABLED` gatea TODA la superficie de
    # `/api/v1/packages/**`, lectura incluida -- con el flag apagado no
    # existe la saga de publicacion, asi que un catalogo de lectura de un
    # feature que no se puede operar no tiene proposito (y "vacio" no es
    # lo mismo que "deshabilitado"). Mismo 404 uniforme que las rutas de
    # escritura, nunca montado.
    if resolved_settings.campaign_packages_enabled:
        package_asset_store = _build_creative_asset_store(container, resolved_settings)
        app.include_router(
            build_package_read_router(container.session_factory, package_asset_store)
        )
        app.include_router(
            build_package_admin_router(
                container.session_factory,
                container.clock,
                package_asset_store,
                container.approval_key_pair.signer,
                # `resume`/`undo` (T031, T025): `PauseEntity` ya cableado con
                # freno/guardarrailes/chokepoint reales -- una fabrica en vez
                # de una instancia porque cada peticion necesita su propia
                # `session` (freno y guardarrailes se evaluan DENTRO de esa
                # transaccion, threat-model.md C-15).
                lambda session: container.build_execution_use_cases(session).pause_entity,
                enabled_google_channels=resolved_settings.google_channels_enabled,
            )
        )
    app.include_router(_build_settings_router(resolved_settings, container))
    app.include_router(build_catalog_router(container.session_factory, container.clock))
    app.include_router(
        build_execution_read_router(
            RequestScopedExecutionReadPort(container.session_factory),
            ContainerSingleExecutionUndoAdapter(container),
        )
    )
    # `optimization`: los 4 puertos que solo tenian doble en memoria ya
    # tienen adaptador SQL real (`optimization/infrastructure/`,
    # `optimization/presentation/rest.py::build_optimization_router_over_sql`).
    app.include_router(
        build_optimization_router_over_sql(container.session_factory, container.clock)
    )

    # --- lane: oauth-connect (cableado) ---
    # Rutas de conexion de cuentas de CLIENTE (OAuth "Conectar", revoke, token
    # de sistema de Meta): el token nunca pasa por ads-api, vive cifrado en el
    # broker (threat-model.md C-24).
    from safent_ads.accounts.presentation.connections_router import (  # noqa: PLC0415
        build_connections_router,
    )

    app.include_router(build_connections_router(resolved_settings))
    # --- end lane: oauth-connect (cableado) ---

    # --- lane: app-credentials-ui ---
    # Rutas de credenciales de VENDOR (cliente OAuth de Google, app
    # de Meta): el propietario las teclea desde el panel, nunca desde
    # `vendor.env` (owner decision).
    _include_platform_apps_router(app, resolved_settings)
    # --- end lane: app-credentials-ui ---

    # --- spec 008 fase D: topes duros desde el panel ---
    _include_hard_caps_router(app, resolved_settings)
    # --- end spec 008 fase D ---

    # --- lane: cloudflare-ui (006, owner decision) ---
    _include_cloudflare_connection_router(app, container, resolved_settings)
    # --- end lane: cloudflare-ui ---

    # --- lane: onboarding (029 T022) ---
    _include_onboarding_router(app, resolved_settings)
    # --- end lane: onboarding ---

    # `panel_spa` va el ultimo: catch-all de rutas no-API, para que
    # `/api/v1/*`/`/mcp` sigan ganando por orden de registro.
    _mount_panel_spa(
        app, resolved_settings.panel_dist_dir, InstanceIdentity.from_settings(resolved_settings)
    )

    # Perf (medido 16-sep en la instancia de produccion, item 1): comprime el
    # panel (JS/CSS/HTML) y las respuestas JSON de la API cuando el
    # cliente acepta gzip. Registrado el ultimo -- por tanto envoltura mas
    # EXTERNA (`Starlette.add_middleware` inserta al frente, ver el
    # docstring de `_add_hardening_middlewares` en `composition/api.py`) --
    # asi comprime la respuesta final tal cual sale, cabeceras de
    # seguridad incluidas. `GZipMiddleware` ya excluye
    # `text/event-stream` por defecto, asi que el transporte MCP
    # streamable-http no se ve afectado. Cubre el modo companion (sin
    # Caddy delante): este `create_app` es el mismo en ambos modos, solo
    # `managed_central` (`create_managed_app`, sin panel ni JSON pesado)
    # se sirve sin el.
    app.add_middleware(GZipMiddleware, minimum_size=_GZIP_MINIMUM_SIZE_BYTES)

    return app


def _include_platform_apps_router(app: FastAPI, settings: ApiSettings) -> None:
    from safent_ads.accounts.presentation.platform_apps_router import (  # noqa: PLC0415
        build_platform_apps_router,
    )

    app.include_router(build_platform_apps_router(settings))


def _include_hard_caps_router(app: FastAPI, settings: ApiSettings) -> None:
    """Inerte hasta que alguien declare `panel_managed:` en
    `config/caps.yaml`: sin sobre, todo PUT responde 409
    `ENVELOPE_NOT_DECLARED` y el comportamiento del despliegue queda
    identico al de antes de spec 008."""
    from safent_ads.accounts.presentation.hard_caps_router import (  # noqa: PLC0415
        build_hard_caps_router,
    )

    app.include_router(
        build_hard_caps_router(settings, federated_available=settings.federated_login_active)
    )


def _route_mcp_transport(app: FastAPI, mcp_router: SeatCredentialRouter) -> None:
    """Engancha el transporte streamable-http en el path EXACTO que publica
    el contrato (`https://<host>/mcp`, sin barra final).

    `app.mount("/mcp", mcp_router)` no sirve: el regex de `Mount` es
    `^/mcp/(?P<path>.*)$`, asi que el `/mcp` pelado -- el que trae
    `companions.json`, el `argv` de `npx mcp-remote` y contracts/mcp.md --
    nunca llega a la sub-app y se queda en el `307` de `redirect_slashes`
    hacia `/mcp/`. El cliente que no sigue redirecciones en un POST se queda
    esperando una respuesta MCP que no llega; el que si las sigue paga dos
    peticiones (y dos fichas del limite de tasa de `/mcp`) por mensaje y
    pierde el flujo SSE de servidor a cliente del `GET`.

    `Route` con una app ASGI como `endpoint` casa el path exacto y sin
    filtrar por metodo (`methods=None`), que es justo lo que exige
    streamable-http: `POST`, `GET` y `DELETE` sobre la misma URL.

    `ContentLengthLimitMiddleware` (M-4) envuelve el enrutador ENTERO, no
    solo un sub-app de permiso: el tope de cuerpo aplica antes de que
    `SeatCredentialRouter` resuelva ninguna credencial.
    """
    app.router.routes.append(
        Route(MCP_ENDPOINT_PATH, endpoint=ContentLengthLimitMiddleware(mcp_router))
    )


def _build_lifespan(
    container: Container,
    mcp_apps: Mapping[Permission, Starlette],
    caller_scope_resolver: CallerScopeResolverPort,
) -> Lifespan[FastAPI]:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("ads_api_starting")
        async with AsyncExitStack() as stack:
            # Tres sub-apps (004 tasks.md A6), cada una con su propio
            # `StreamableHTTPSessionManager`: Starlette no reenvia el evento
            # ASGI `lifespan` a una sub-app montada como `Route`, asi que
            # cada una entra explicitamente en su propio `lifespan_context`.
            for mcp_app in mcp_apps.values():
                await stack.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
            # T127 "credential health counts": bucle de fondo propio, no un
            # cron externo (T126 sigue pendiente) -- se detiene con el
            # mismo `stop_event` en el `finally` de abajo, nunca un task
            # huerfano tras `ads_api_stopped`.
            stop_event = asyncio.Event()
            health_task = asyncio.create_task(
                run_credential_health_refresh_forever(
                    container.session_factory, stop_event=stop_event
                )
            )
            # Spec 002 (mcp_oauth) tasks.md T016: mismo patron de
            # `stop_event` compartido -- ambos bucles de fondo drenan limpio
            # en el mismo `finally`, ninguno queda huerfano tras
            # `ads_api_stopped`.
            prune_task = asyncio.create_task(
                run_prune_stale_oauth_state_forever(
                    container.session_factory, clock=container.clock, stop_event=stop_event
                )
            )
            # D-11 (threat-model.md C-70 pieza 4): delata las registraciones
            # antiguas con destino remoto, que la regla nueva deja sin poder
            # autorizar. No las borra ni las arregla: avisa una vez. En una
            # tarea propia -- es informativa, y dar por listo el servicio no
            # puede esperar a una consulta.
            audit_task = asyncio.create_task(
                audit_client_redirect_uris(container.session_factory)
            )
            try:
                yield
            finally:
                stop_event.set()
                await health_task
                await prune_task
                await audit_task
                aclose = getattr(caller_scope_resolver, "aclose", None)
                if aclose is not None:
                    await aclose()
                await container.aclose()
                logger.info("ads_api_stopped")

    return lifespan


def _mount_health(app: FastAPI) -> None:
    @app.get("/api/v1/health", include_in_schema=True)
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Entrypoint del proceso `ads-api` (Containerfile/compose.yaml:
# `python -m safent_ads.composition.app`, mismo patron que
# `composition/worker.py`/`composition/broker.py`). Fuera de companion
# mode nada cambia: `127.0.0.1:8410` en claro, exactamente como hoy. En
# companion mode (`ADS_COMPANION_MODE=true`, diseñado en el runtime)
# `ApiSettings` ya exigio
# TLS legible al construirse (fail closed, `composition/settings.py`), asi
# que aqui solo queda elegir el binding.
# ---------------------------------------------------------------------------

# T127: puerto fijo, SIEMPRE en loopback dentro del contenedor
# (`observability/server.py`) -- nunca en `compose.yaml:ports` ni en
# `compose.companion.yaml`, ni en modo companion ni fuera de el. Mismo
# puerto en `composition/worker.py`/`composition/broker.py`: cada uno es
# un contenedor/netns distinto, no hay colision real.
_METRICS_PORT = 9410
_PLAINTEXT_PORT = 8410
# La red fija del companion (10.201.0.0/24, spec.md INV-3): el proceso
# escucha en todas las interfaces del contenedor porque el runtime lo
# alcanza por su IP fija (10.201.0.10), no por loopback.
_COMPANION_HOST = "0.0.0.0"  # noqa: S104 - red interna fija del companion, sin publicar puerto en el host
_COMPANION_PORT = 8443


@dataclass(frozen=True, slots=True)
class _ServerBinding:
    host: str
    port: int
    ssl_certfile: str | None
    ssl_keyfile: str | None


def _server_binding(settings: ApiSettings) -> _ServerBinding:
    if not settings.companion_mode:
        return _ServerBinding(
            host=settings.bind_host, port=_PLAINTEXT_PORT, ssl_certfile=None, ssl_keyfile=None
        )
    # `ApiSettings` ya valido que ambos ficheros existen y son legibles
    # (`_require_readable_tls_material_in_companion_mode`): aqui no se
    # repite esa comprobacion, solo se traduce a los kwargs de uvicorn.
    return _ServerBinding(
        host=_COMPANION_HOST,
        port=_COMPANION_PORT,
        ssl_certfile=str(settings.tls_certfile),
        ssl_keyfile=str(settings.tls_keyfile),
    )


def main() -> None:
    settings = ApiSettings()  # type: ignore[call-arg]
    binding = _server_binding(settings)
    start_metrics_server(_METRICS_PORT)
    uvicorn.run(
        "safent_ads.composition.app:create_app",
        factory=True,
        host=binding.host,
        port=binding.port,
        ssl_certfile=binding.ssl_certfile,
        ssl_keyfile=binding.ssl_keyfile,
    )


if __name__ == "__main__":
    main()
